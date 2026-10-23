"""ESMA FIRDS equity reference data for the EU venues (CAP-CO-03, CAP-INS-01; registry ``esma-firds``).

Source: the FIRDS file list (``registers.esma.europa.eu/solr/esma_registers_firds_files``, JSON) names
the weekly full files; this module takes the newest ``FULINS_E_*`` set (equities), downloads each zip
(``download_link`` exactly as listed, never a guessed name) and stream-parses the ISO 20022
``auth.017`` XML inside it. Elements are matched by local name, so a schema version change in the
namespace does not silently drop records. A prolog with a DTD or entity declaration is refused.

Kept: records whose trading venue (``TradgVnRltdAttrbts/Id``) is one of the EU equity MICs in
``config/markets/venues.json`` (``macro_common.eu_equity_mics``). Each (ISIN, MIC) becomes an entity
``FIRDS-<ISIN>-<MIC>`` of kind ``eu_instrument`` with ISIN, MIC, CFI, full and short name, notional
currency, issuer LEI, first trade and termination dates; and one observation (field
``reference``, value_text = the record as canonical JSON) so a later snapshot with different values is
a new publication flagged ``revised_after_first_seen``. Reference data is available from receipt.

Status 2026-10-23: the register host answered HTTP 502 to every request, so no live file has been
parsed; the parser is tested on a synthetic auth.017 document.

  python -m unknown.data.sources.firds ingest [--date YYYY-MM-DD]
"""
from __future__ import annotations

import argparse
import io
import json
import time
import zipfile
from datetime import date, timedelta
from urllib.parse import urlencode
from xml.etree.ElementTree import iterparse

from ...domain.clock import ns_to_iso
from ..pit import Observation
from .common import record_if_changed
from .macro_common import eu_equity_mics, require_storage

PROVIDER = "esma-firds"
DATASET = "esma.firds_equity"
FILES = "https://registers.esma.europa.eu/solr/esma_registers_firds_files/select"
ATTRIBUTION = "Source: ESMA Financial Instruments Reference Data System (FIRDS)"
FIELDS = ("isin", "mic", "cfi", "full_name", "short_name", "currency", "issuer_lei", "first_trade_date", "termination_date")


def files_url(day: date, lookback_days: int = 8) -> str:
    start = day - timedelta(days=lookback_days)
    fq = f"publication_date:[{start.isoformat()}T00:00:00Z TO {day.isoformat()}T23:59:59Z]"
    return FILES + "?" + urlencode({"q": "*", "fq": fq, "wt": "json", "indent": "false", "start": "0", "rows": "500"})


def parse_file_list(body: bytes) -> list[dict]:
    docs = (json.loads(body).get("response") or {}).get("docs") or []
    out = [{"file_name": d.get("file_name"), "file_type": d.get("file_type"), "download_link": d.get("download_link"),
            "publication_date": (d.get("publication_date") or "")[:10]} for d in docs]
    return [d for d in out if d["file_name"] and d["download_link"]]


def newest_equity_full_set(files: list[dict]) -> list[dict]:
    full = [f for f in files if f["file_name"].startswith("FULINS_E_")]
    if not full:
        return []
    newest = max(f["file_name"].split("_")[2] for f in full)
    return sorted((f for f in full if f["file_name"].split("_")[2] == newest), key=lambda f: f["file_name"])


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _guard_prolog(head: bytes) -> None:
    u = head.upper()
    if b"<!DOCTYPE" in u or b"<!ENTITY" in u:
        raise ValueError("XML with a DTD or entity declarations is refused")


def parse_xml(stream, mics: frozenset[str]) -> tuple[list[dict], dict]:
    """(records, stats) from an auth.017 document; only records on ``mics``."""
    head = stream.read(65536)
    _guard_prolog(head)
    stream = io.BufferedReader(_Chain(head, stream))
    recs, stats = [], {"records": 0, "kept": 0, "other_venue": 0, "incomplete": 0}
    for _, el in iterparse(stream, events=("end",)):
        if _local(el.tag) != "RefData":
            continue
        stats["records"] += 1
        r = {k: None for k in FIELDS}
        for sub in el.iter():
            name = _local(sub.tag)
            parent_text = (sub.text or "").strip()
            if name == "FinInstrmGnlAttrbts":
                for g in sub:
                    n = _local(g.tag)
                    v = (g.text or "").strip()
                    r.update({"Id": {"isin": v}, "FullNm": {"full_name": v}, "ShrtNm": {"short_name": v}, "ClssfctnTp": {"cfi": v},
                              "NtnlCcy": {"currency": v}}.get(n, {}))
            elif name == "Issr":
                r["issuer_lei"] = parent_text
            elif name == "TradgVnRltdAttrbts":
                for g in sub:
                    n = _local(g.tag)
                    v = (g.text or "").strip()
                    r.update({"Id": {"mic": v}, "FrstTradDt": {"first_trade_date": v[:10]}, "TermntnDt": {"termination_date": v[:10]}}.get(n, {}))
        el.clear()
        if not r["isin"] or not r["mic"]:
            stats["incomplete"] += 1
            continue
        if r["mic"] not in mics:
            stats["other_venue"] += 1
            continue
        recs.append(r)
    stats["kept"] = len(recs)
    return recs, stats


class _Chain(io.RawIOBase):
    """A readable stream that replays ``head`` and then continues with ``rest``."""

    def __init__(self, head: bytes, rest):
        self.head, self.rest = head, rest

    def readable(self) -> bool:
        return True

    def readinto(self, b) -> int:
        if self.head:
            n = min(len(b), len(self.head))
            b[:n], self.head = self.head[:n], self.head[n:]
            return n
        data = self.rest.read(len(b))
        b[:len(data)] = data
        return len(data)


def entity_id(isin: str, mic: str) -> str:
    return f"FIRDS-{isin}-{mic}"


def store_records(store, recs: list[dict], *, raw_sha: str, receipt_ns: int, now_ns, file_name: str) -> dict:
    counts = {"NEW": 0, "UNCHANGED": 0, "REVISED": 0, "CORRECTED": 0}
    for r in recs:
        eid = entity_id(r["isin"], r["mic"])
        store.upsert_entity(eid, "eu_instrument", r["full_name"] or r["isin"], {**r, "source": "esma.firds", "file_name": file_name})
        obs = Observation(dataset=DATASET, entity_id=eid, field="reference", unit="record", value_text=json.dumps(r, sort_keys=True),
                          receipt_time_ns=receipt_ns, available_time_ns=receipt_ns, source_ref=f"raw:{raw_sha}#{r['isin']}/{r['mic']}",
                          publication_id=f"firds:{r['isin']}:{r['mic']}", quality=(f"file:{file_name}",))
        _, outcome = record_if_changed(store, obs, now_ns())
        counts[outcome] += 1
    return counts


def ingest(store, fetcher, now_ns, *, day: date | None = None) -> dict:
    require_storage(DATASET)
    day = day or date.today()
    mics = eu_equity_mics()
    lst = fetcher.get(files_url(day))
    files = newest_equity_full_set(parse_file_list(lst.body))
    out = {"provider": PROVIDER, "dataset": DATASET, "mics": sorted(mics), "files": [], "NEW": 0, "UNCHANGED": 0, "REVISED": 0}
    if not files:
        return {**out, "status": "NOT_AVAILABLE", "reason": f"no FULINS_E file listed in the 8 days to {day.isoformat()}"}
    for f in files:
        resp = fetcher.get(f["download_link"])
        sha = store.put_raw(resp.body, provider=PROVIDER, dataset=DATASET, request={"url": resp.url}, receipt_time_ns=resp.receipt_time_ns,
                            media_type=resp.content_type or "application/zip")
        with zipfile.ZipFile(io.BytesIO(resp.body)) as z:
            member = next(n for n in z.namelist() if n.lower().endswith(".xml"))
            with z.open(member) as xml:
                recs, stats = parse_xml(xml, mics)
        counts = store_records(store, recs, raw_sha=sha, receipt_ns=resp.receipt_time_ns, now_ns=now_ns, file_name=f["file_name"])
        for k in ("NEW", "UNCHANGED", "REVISED"):
            out[k] += counts[k]
        out["files"].append({"file_name": f["file_name"], "raw_sha256": sha, "receipt_time": ns_to_iso(resp.receipt_time_ns), **stats, **counts})
    return {**out, "status": "OK"}


def instruments_for_issuer(store, lei: str) -> list[dict]:
    rows = store.conn.execute("SELECT entity_id, attributes FROM entity WHERE kind='eu_instrument' AND attributes LIKE ?",
                              (f'%"issuer_lei": "{lei}"%',)).fetchall()
    return sorted((json.loads(r["attributes"]) for r in rows), key=lambda a: (a.get("isin") or "", a.get("mic") or ""))


def instruments_by_isin(store, isin: str) -> list[dict]:
    rows = store.conn.execute("SELECT attributes FROM entity WHERE kind='eu_instrument' AND entity_id LIKE ?", (f"FIRDS-{isin}-%",)).fetchall()
    return [json.loads(r["attributes"]) for r in rows]


def main(argv: list[str] | None = None) -> None:
    from .common import data_config, make_fetcher
    from .runner import open_store
    ap = argparse.ArgumentParser(description="Ingest ESMA FIRDS equity reference data for the EU venues")
    sub = ap.add_subparsers(dest="cmd", required=True)
    ing = sub.add_parser("ingest")
    ing.add_argument("--date", default=None, help="YYYY-MM-DD (default today)")
    ing.add_argument("--home", default=None)
    a = ap.parse_args(argv)
    store, home = open_store(a.home)
    fetcher = make_fetcher(PROVIDER, data_config(home))
    print(json.dumps(ingest(store, fetcher, time.time_ns, day=date.fromisoformat(a.date) if a.date else None), indent=2))


if __name__ == "__main__":
    main()
