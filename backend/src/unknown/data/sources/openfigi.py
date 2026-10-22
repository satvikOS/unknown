"""OpenFIGI identifier mapping (CAP-DATA-04, CAP-INS-02; registry ``openfigi``).

API: ``POST https://api.openfigi.com/v3/mapping`` with a JSON array of jobs
``{"idType", "idValue", optional "exchCode"/"micCode"/"currency"/"marketSecDes"/...}``; the response is
one element per job: ``{"data": [...]}``, ``{"warning": "No identifier found."}`` or ``{"error": ...}``
(the documentation shows both forms for "no match"; both are handled). Documented limits
(CAP-DATA-04): without an API key 25 mapping requests per 60 s and 10 jobs per request; with a key
250 per 60 s and 100 jobs. HTTP 429 -> wait the ``retry-after`` seconds; HTTP 413 -> split the batch.
The limiter is a strict sliding 60-second window (a token bucket could burst past the window).
The optional key is read from ``~/.unknown/config/data.json`` (``openfigi_api_key``) and sent only in
the ``X-OPENFIGI-APIKEY`` header, never logged or stored.

Before any network call each job is validated: known idType, ISIN check digit, FIGI format.
CUSIP and SEDOL inputs are licensed identifiers (CAP-INS-03) and are refused unless the caller
passes ``allow_licensed=True`` after recording a licence.

Every job outcome is stored, including "no match": dataset ``openfigi.mapping``, entity
``OPENFIGI-JOB:<idType>:<idValue>[:<exchCode>]``, ``value_text`` = canonical JSON of
{status, candidates, message}, available at receipt time. Status is MAPPED (one candidate),
MULTIPLE_CANDIDATES (never auto-picked; a candidate on the requested exchange is marked
``preferred_by_exchange`` but still needs owner confirmation), NO_MATCH or ERROR. Each FIGI becomes
a ``listing`` entity ``FIGI-<figi>`` with a ``figi`` alias. FIGIs are public domain (OpenFIGI terms).

  python -m unknown.data.sources.openfigi map --isin US0378331005
"""
from __future__ import annotations

import argparse
import json
import time
import urllib.error
import urllib.request
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from ...domain.clock import ns_to_iso
from ..http import FetchError
from ..pit import OPEN_END, Observation, ResearchStore
from ..registry import dataset, load_registry
from .common import data_config, declared_user_agent, record_if_changed
from .identifiers import cusip_valid, figi_format_ok, isin_valid, sedol_valid

PROVIDER = "openfigi"
DATASET = "openfigi.mapping"
MAPPING_URL = "https://api.openfigi.com/v3/mapping"
WINDOW_S = 60.0
LIMITS = {False: {"mapping_requests": 25, "jobs_per_request": 10}, True: {"mapping_requests": 250, "jobs_per_request": 100}}
ID_TYPES = frozenset({"ID_ISIN", "TICKER", "ID_BB_GLOBAL", "COMPOSITE_ID_BB_GLOBAL", "ID_BB_GLOBAL_SHARE_CLASS_LEVEL",
                      "ID_EXCH_SYMBOL", "ID_CUSIP", "ID_SEDOL"})
LICENSED_ID_TYPES = frozenset({"ID_CUSIP", "ID_SEDOL"})
JOB_KEYS = frozenset({"idType", "idValue", "exchCode", "micCode", "currency", "marketSecDes", "securityType",
                      "securityType2", "includeUnlistedEquities"})
CANDIDATE_KEYS = ("figi", "compositeFIGI", "shareClassFIGI", "name", "ticker", "exchCode", "marketSector",
                  "securityType", "securityType2", "securityDescription")
NO_MATCH_TEXT = "no identifier found"


class JobError(ValueError):
    pass


class PayloadTooLarge(RuntimeError):
    pass


def validate_job(job: dict, *, allow_licensed: bool = False) -> dict:
    if not isinstance(job, dict):
        raise JobError("a job must be an object")
    unknown = set(job) - JOB_KEYS
    if unknown:
        raise JobError(f"unknown job fields {sorted(unknown)}")
    id_type, value = job.get("idType"), job.get("idValue")
    if id_type not in ID_TYPES:
        raise JobError(f"idType {id_type!r} not supported (one of {sorted(ID_TYPES)})")
    if not isinstance(value, str) or not value.strip():
        raise JobError("idValue is required")
    value = value.strip().upper() if id_type != "TICKER" else value.strip()
    if id_type in LICENSED_ID_TYPES and not allow_licensed:
        raise JobError(f"{id_type} is a licensed identifier (CAP-INS-03); refused without a recorded licence")
    checks = {"ID_ISIN": isin_valid, "ID_CUSIP": cusip_valid, "ID_SEDOL": sedol_valid, "ID_BB_GLOBAL": figi_format_ok,
              "COMPOSITE_ID_BB_GLOBAL": figi_format_ok, "ID_BB_GLOBAL_SHARE_CLASS_LEVEL": figi_format_ok}
    if id_type in checks and not checks[id_type](value):
        raise JobError(f"invalid {id_type} {value!r} (check digit or format); not sent")
    return {**job, "idValue": value}


def job_entity(job: dict) -> str:
    parts = ["OPENFIGI-JOB", job["idType"], job["idValue"]]
    for k in ("exchCode", "micCode", "currency", "marketSecDes"):
        if job.get(k):
            parts.append(f"{k}={job[k]}")
    return ":".join(parts)


def retry_after_seconds(headers) -> float | None:
    if headers is None:
        return None
    for k in headers.keys():
        if k.lower() == "retry-after":
            try:
                return max(0.0, float(headers[k]))
            except (TypeError, ValueError):
                return None
    return None


class SlidingWindow:
    """At most ``max_requests`` sends in any ``window_s`` seconds."""

    def __init__(self, max_requests: int, window_s: float, monotonic: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep):
        self.max, self.window, self.monotonic, self.sleep = max_requests, window_s, monotonic, sleep
        self.sent: deque[float] = deque()

    def acquire(self) -> None:
        while True:
            now = self.monotonic()
            while self.sent and now - self.sent[0] >= self.window:
                self.sent.popleft()
            if len(self.sent) < self.max:
                self.sent.append(now)
                return
            self.sleep(self.window - (now - self.sent[0]))


@dataclass(frozen=True)
class BatchResult:
    jobs: tuple[dict, ...]
    results: tuple[dict, ...]
    body: bytes
    receipt_time_ns: int
    url: str


class OpenFigiClient:
    def __init__(self, user_agent: str, api_key: str | None = None, *, opener: Callable | None = None,
                 monotonic: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep,
                 clock_ns: Callable[[], int] = time.time_ns, timeout_s: float = 30.0, max_retries: int = 4):
        if not user_agent or "@" not in user_agent:
            raise FetchError("a declared User-Agent with contact details is required (local config)")
        self.user_agent, self._key = user_agent, api_key or None
        limits = LIMITS[self._key is not None]
        self.jobs_per_request = limits["jobs_per_request"]
        self.window = SlidingWindow(limits["mapping_requests"], WINDOW_S, monotonic, sleep)
        self.opener = opener or urllib.request.urlopen
        self.sleep, self.clock_ns, self.timeout_s, self.max_retries = sleep, clock_ns, timeout_s, max_retries
        self.requests_sent = 0

    @property
    def keyed(self) -> bool:
        return self._key is not None

    def _post(self, url: str, payload) -> tuple[bytes, int]:
        data = json.dumps(payload).encode()
        headers = {"Content-Type": "application/json", "User-Agent": self.user_agent, "Accept-Encoding": "identity"}
        if self._key:
            headers["X-OPENFIGI-APIKEY"] = self._key
        delay, last = 1.0, ""
        for attempt in range(self.max_retries + 1):
            self.window.acquire()
            self.requests_sent += 1
            req = urllib.request.Request(url, data=data, headers=headers, method="POST")
            try:
                with self.opener(req, timeout=self.timeout_s) as resp:
                    return resp.read(), self.clock_ns()
            except urllib.error.HTTPError as exc:
                wait = retry_after_seconds(exc.headers)
                exc.close()
                if exc.code == 413:
                    raise PayloadTooLarge(url) from None
                if exc.code == 429:
                    last = "HTTP 429"
                    self.sleep(wait if wait is not None else WINDOW_S)
                    continue
                if exc.code in (500, 502, 503, 504):
                    last = f"HTTP {exc.code}"
                else:
                    raise FetchError(f"HTTP {exc.code} for {url}") from None
            except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
                last = f"{type(exc).__name__}: {exc}"
            if attempt < self.max_retries:
                self.sleep(delay)
                delay = min(delay * 2, 60.0)
        raise FetchError(f"giving up on {url} after {self.max_retries + 1} attempts: {last}")

    def _send(self, jobs: list[dict]) -> list[BatchResult]:
        try:
            body, receipt = self._post(MAPPING_URL, jobs)
        except PayloadTooLarge:
            if len(jobs) == 1:
                raise FetchError("HTTP 413 for a single mapping job") from None
            mid = len(jobs) // 2
            return self._send(jobs[:mid]) + self._send(jobs[mid:])
        results = json.loads(body)
        if not isinstance(results, list) or len(results) != len(jobs):
            raise FetchError("OpenFIGI response does not have one result per job")
        return [BatchResult(tuple(jobs), tuple(results), body, receipt, MAPPING_URL)]

    def map(self, jobs: list[dict], *, allow_licensed: bool = False) -> list[BatchResult]:
        valid = [validate_job(j, allow_licensed=allow_licensed) for j in jobs]   # all validated before any call
        out: list[BatchResult] = []
        for i in range(0, len(valid), self.jobs_per_request):
            out += self._send(valid[i:i + self.jobs_per_request])
        return out


def classify_result(job: dict, result: dict) -> dict:
    if "data" in result and result["data"]:
        cands = sorted(({k: c.get(k) for k in CANDIDATE_KEYS} for c in result["data"]), key=lambda c: c.get("figi") or "")
        out = {"status": "MAPPED" if len(cands) == 1 else "MULTIPLE_CANDIDATES", "candidates": cands, "selected": None}
        if len(cands) > 1 and job.get("exchCode"):
            pref = [c["figi"] for c in cands if c.get("exchCode") == job["exchCode"]]
            if len(pref) == 1:
                out["preferred_by_exchange"] = pref[0]
        return out
    message = result.get("warning") or result.get("error") or ("empty data" if "data" in result else "unrecognised response")
    status = "NO_MATCH" if NO_MATCH_TEXT in str(message).lower() or ("data" in result and not result["data"]) else "ERROR"
    return {"status": status, "candidates": [], "selected": None, "message": message}


def ingest_mapping(store: ResearchStore, client: OpenFigiClient, jobs: list[dict], now_ns, *, allow_licensed: bool = False) -> dict:
    ds = dataset(load_registry(), DATASET)
    if not ds.permits("storage"):
        raise PermissionError(f"{DATASET}: storage not permitted by the registry")
    counts = {"NEW": 0, "UNCHANGED": 0, "REVISED": 0, "CORRECTED": 0}
    statuses: dict[str, int] = {}
    outcomes = []
    for batch in client.map(jobs, allow_licensed=allow_licensed):
        sha = store.put_raw(batch.body, provider=PROVIDER, dataset=DATASET, request={"url": batch.url, "jobs": list(batch.jobs)},
                            receipt_time_ns=batch.receipt_time_ns, media_type="application/json")
        for i, (job, result) in enumerate(zip(batch.jobs, batch.results)):
            outcome = classify_result(job, result)
            statuses[outcome["status"]] = statuses.get(outcome["status"], 0) + 1
            obs = Observation(dataset=DATASET, entity_id=job_entity(job), field="mapping", unit="json",
                              value_text=json.dumps(outcome, sort_keys=True), period_kind="SNAPSHOT",
                              receipt_time_ns=batch.receipt_time_ns, available_time_ns=batch.receipt_time_ns,
                              publication_id=f"openfigi:{sha}", source_ref=f"raw:{sha}#job{i}",
                              quality=(f"keyed:{client.keyed}",))
            obs_id, rec = record_if_changed(store, obs, now_ns())
            counts[rec] = counts.get(rec, 0) + 1
            for c in outcome["candidates"]:
                if not c.get("figi"):
                    continue
                eid = f"FIGI-{c['figi']}"
                store.upsert_entity(eid, "listing", c.get("name") or c["figi"], {k: v for k, v in c.items() if v is not None})
                if store.resolve("figi", c["figi"], batch.receipt_time_ns) is None:
                    store.add_alias("figi", c["figi"], eid, batch.receipt_time_ns, f"raw:{sha}#job{i}")
            outcomes.append({"job": job, "status": outcome["status"], "candidates": len(outcome["candidates"]), "obs_id": obs_id})
    return {"provider": PROVIDER, "dataset": DATASET, "jobs": len(jobs), "requests_sent": client.requests_sent,
            "keyed": client.keyed, "statuses": statuses, "outcomes": outcomes, **counts}


def mappings_as_of(store, as_of_ns: int) -> list[dict]:
    """Latest stored outcome per job available at ``as_of_ns`` (current knowledge)."""
    rows = store.conn.execute(
        "SELECT entity_id, value_text, available_time_ns, obs_id FROM observation WHERE dataset=? AND field='mapping' "
        "AND available_time_ns<=? AND known_to_ns=? ORDER BY available_time_ns", (DATASET, as_of_ns, OPEN_END)).fetchall()
    latest: dict[str, dict] = {}
    for r in rows:
        latest[r["entity_id"]] = {"job": r["entity_id"], "outcome": json.loads(r["value_text"]), "obs_id": r["obs_id"],
                                  "retrieved_at": ns_to_iso(r["available_time_ns"])}
    return list(latest.values())


def main(argv: list[str] | None = None) -> None:
    from ...service.paths import Paths
    ap = argparse.ArgumentParser(description="Map identifiers to FIGIs with OpenFIGI and store the outcomes")
    sub = ap.add_subparsers(dest="cmd", required=True)
    m = sub.add_parser("map")
    m.add_argument("--isin", action="append", default=[])
    m.add_argument("--ticker", action="append", default=[], help="TICKER or TICKER:EXCHCODE (e.g. AAPL:US)")
    m.add_argument("--home", default=None)
    a = ap.parse_args(argv)
    jobs = [{"idType": "ID_ISIN", "idValue": v} for v in a.isin]
    for t in a.ticker:
        sym, _, exch = t.partition(":")
        jobs.append({"idType": "TICKER", "idValue": sym, **({"exchCode": exch} if exch else {})})
    if not jobs:
        ap.error("give at least one --isin or --ticker")
    home = Path(a.home).expanduser() if a.home else None
    paths = Paths(home) if home else Paths.default()
    cfg = data_config(home)
    research = paths.root / "research"
    research.mkdir(parents=True, exist_ok=True)
    store = ResearchStore(research / "research.sqlite")
    client = OpenFigiClient(declared_user_agent(cfg), cfg.get("openfigi_api_key"))
    print(json.dumps(ingest_mapping(store, client, jobs, time.time_ns), indent=2))


if __name__ == "__main__":
    main()
