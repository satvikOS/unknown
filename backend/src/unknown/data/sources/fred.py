"""FRED series observations (CAP-DATA-05; registry ``fred``, dataset ``fred.series``). INACTIVE without a key.

Activation: the owner registers a free FRED API key and writes it, alone on one line, to
``<UNKNOWN_HOME>/secrets/fred_api_key`` (default ``~/.unknown/secrets/fred_api_key``, outside git).
Without that file ``status()`` reports INACTIVE with the file name and ``ingest`` makes no request.

The key is a credential: it is sent only in the request URL FRED requires, and it is redacted
(``api_key=REDACTED``) from the raw-request record, every returned URL and every error message.

Source: ``https://api.stlouisfed.org/fred/series/observations?series_id=<ID>&api_key=<KEY>&file_type=json``.
Each observation is ``{"date": "YYYY-MM-DD", "value": "<decimal text>" | "."}``; "." (no value)
is stored as NOT_REPORTED, never zero. Default series: DFEDTARU, DFEDTARL (US policy range),
UNRATE, A191RL1Q225SBEA, CPIAUCSL (``macro_common.FRED_SERIES``). The period of an observation is
its frequency (a monthly series' date is the first day of the month). Current vintage only;
availability is first capture. Attribution notice: ``macro_common.FRED_ATTR``.

  python -m unknown.data.sources.fred status
  python -m unknown.data.sources.fred ingest --from 2015-01-01 [--series UNRATE ...]
"""
from __future__ import annotations

import argparse
import json
import re
import time
from datetime import date
from pathlib import Path
from urllib.parse import urlencode

from ...domain.clock import ns_to_iso
from ..http import FetchError
from . import macro_common as mc
from .common import data_config, make_fetcher

PROVIDER = "fred"
DATASET = "fred.series"
KEY_FILE = "fred_api_key"
BASE = "https://api.stlouisfed.org/fred/series/observations"
DEFAULT_SERIES = tuple(mc.FRED_SERIES)
SERIES_ID = re.compile(r"^[A-Z0-9_]{1,40}$")
FREQ_PERIOD = {"A": lambda d: d[:4], "Q": lambda d: f"{d[:4]}-Q{(int(d[5:7]) - 1) // 3 + 1}", "M": lambda d: d[:7], "D": lambda d: d}


def key_path(home: Path | None = None) -> Path:
    from ...service.paths import Paths
    return (Paths(home) if home else Paths.default()).secrets / KEY_FILE


def read_key(home: Path | None = None) -> str | None:
    p = key_path(home)
    if not p.is_file():
        return None
    key = p.read_text().strip()
    if not re.fullmatch(r"[A-Za-z0-9]{16,64}", key):
        raise ValueError(f"{p} does not hold a FRED API key (expected one line of 16-64 letters and digits)")
    return key


def status(home: Path | None = None) -> dict:
    p = key_path(home)
    if not p.is_file():
        return {"provider": PROVIDER, "status": "INACTIVE", "key_file": str(p),
                "reason": f"FRED is inactive: no key file. Register a free key at fredaccount.stlouisfed.org and write it to {p}"}
    return {"provider": PROVIDER, "status": "ACTIVE", "key_file": str(p), "attribution": mc.FRED_ATTR}


def redact(text: str) -> str:
    return re.sub(r"api_key=[^&\s]+", "api_key=REDACTED", text)


def url(series_id: str, key: str, start: str | None = None) -> str:
    if not SERIES_ID.fullmatch(series_id):
        raise ValueError(f"bad FRED series id {series_id!r}")
    params = {"series_id": series_id, "api_key": key, "file_type": "json"}
    if start:
        date.fromisoformat(start)
        params["observation_start"] = start
    return BASE + "?" + urlencode(params)


def parse(body: bytes, frequency: str) -> tuple[list[dict], list[dict]]:
    doc = json.loads(body.decode("utf-8"))
    if "error_code" in doc or "error_message" in doc:
        raise ValueError(f"FRED error: {redact(str(doc.get('error_message')))[:300]}")
    to_period = FREQ_PERIOD.get(frequency, FREQ_PERIOD["D"])
    rows, skipped = [], []
    for o in doc.get("observations", []):
        d, raw = o.get("date"), (o.get("value") or "").strip()
        try:
            date.fromisoformat(d)
            value = None if raw in ("", ".") else mc.exact(raw)
        except (TypeError, ValueError) as exc:
            skipped.append({"date": d, "reason": str(exc)})
            continue
        rows.append({"period": to_period(d), "value": value})
    return rows, skipped


def ingest(store, fetcher, now_ns, *, home: Path | None = None, series: tuple[str, ...] = DEFAULT_SERIES,
           start: str | None = None) -> dict:
    st = status(home)
    if st["status"] != "ACTIVE":
        return st
    mc.require_storage(DATASET)
    key = read_key(home)
    out = {"provider": PROVIDER, "dataset": DATASET, "series": [], "attribution": mc.FRED_ATTR,
           "NEW": 0, "UNCHANGED": 0, "REVISED": 0, "CORRECTED": 0}
    for sid in series:
        col = mc.fred_column(sid)
        u = url(sid, key, start)
        try:
            resp = fetcher.get(u)
        except FetchError as exc:
            raise FetchError(redact(str(exc))) from None
        sha = store.put_raw(resp.body, provider=PROVIDER, dataset=DATASET, request={"url": redact(resp.url)},
                            receipt_time_ns=resp.receipt_time_ns, media_type=resp.content_type)
        rows, skipped = parse(resp.body, col.frequency)
        pts = [{"period": r["period"], "value": r["value"], "missing_reason": "NOT_REPORTED", "key": sid} for r in rows]
        counts = mc.record_points(store, col, "US", pts, raw_sha=sha, receipt_ns=resp.receipt_time_ns, now_ns=now_ns)
        mc.merge_counts(out, counts)
        out["series"].append({"series_id": sid, "url": redact(u), "raw_sha256": sha, "receipt_time": ns_to_iso(resp.receipt_time_ns),
                              "rows": len(rows), "skipped": len(skipped), **counts})
    return out


def main(argv: list[str] | None = None) -> None:
    from .runner import open_store
    ap = argparse.ArgumentParser(description="FRED series (inactive until <UNKNOWN_HOME>/secrets/fred_api_key exists)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    stp = sub.add_parser("status")
    stp.add_argument("--home", default=None)
    ing = sub.add_parser("ingest")
    ing.add_argument("--from", dest="start", default=None, help="YYYY-MM-DD")
    ing.add_argument("--series", nargs="*", default=list(DEFAULT_SERIES))
    ing.add_argument("--home", default=None)
    a = ap.parse_args(argv)
    home = Path(a.home).expanduser() if a.home else None
    if a.cmd == "status":
        print(json.dumps(status(home), indent=2))
        return
    if status(home)["status"] != "ACTIVE":
        print(json.dumps(status(home), indent=2))
        return
    store, home = open_store(a.home)
    fetcher = make_fetcher(PROVIDER, data_config(home))
    print(json.dumps(ingest(store, fetcher, time.time_ns, home=home, series=tuple(a.series), start=a.start), indent=2))


if __name__ == "__main__":
    main()
