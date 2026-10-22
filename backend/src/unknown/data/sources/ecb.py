"""ECB euro foreign exchange reference rates (CAP-DATA-07, CAP-MAC-12; registry ``ecb``).

Source: ECB Data API, dataflow EXR, series key ``D.<CCY>.EUR.SP00.A`` (daily spot reference rate),
``https://data-api.ecb.europa.eu/service/data/EXR/D..EUR.SP00.A?format=csvdata``. Columns used:
KEY, CURRENCY, CURRENCY_DENOM, TIME_PERIOD, OBS_VALUE, OBS_STATUS, UNIT_MULT, DECIMALS, TITLE.

Orientation: each value is units of CURRENCY per 1 EUR, stored exactly as published (decimal
text, never a float), unit ``<CCY>/EUR``. ``normalize`` turns them into the internal convention
x_{c->B} = units of B per 1 unit of c (book ch. 3, CAP-MAC-12):
x_{EUR->Q} = R_Q, x_{Q->EUR} = 1/R_Q, x_{A->B} = R_B / R_A, computed in exact decimal and rounded
HALF_EVEN only at the declared output scale, and labelled derived.

Use restriction: the ECB publishes these rates "for information purposes only" and says using them
"for transaction purposes is strongly discouraged". They are for valuation and reporting, never for
order pricing or funding checks; nothing in the core imports this module.

Availability: the capture rule in ``common`` with time zone Europe/Berlin (CET/CEST): a live capture
is available when first seen (publication is around 16:00 CET, after the 14:10 concertation); a
backfill is available at 23:59:59 CET on the reference date, flagged. A backtest at 15:00 CET on
date D therefore never reads D's fixing.

  python -m unknown.data.sources.ecb ingest --start 2026-10-01 | --last 1
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import time
from datetime import date
from pathlib import Path
from urllib.parse import urlencode

from ...domain.clock import ns_to_iso
from ...domain.fixed import Fixed, Rounding
from ..pit import Observation, ResearchStore
from ..registry import dataset, load_registry
from .common import capture_availability, data_config, make_fetcher, record_if_changed

PROVIDER = "ecb"
DATASET = "ecb.exr_reference"
SERIES = "https://data-api.ecb.europa.eu/service/data/EXR/D.{ccy}.EUR.SP00.A"
TZ = "Europe/Berlin"
FIELD = "reference_rate"
OUTPUT_SCALE = 10
ATTRIBUTION = "Source: ECB statistics (euro foreign exchange reference rates); derived cross rates are modifications by this system"
USE_NOTE = ("ECB reference rates are published for information purposes only; using them for transaction purposes is "
            "strongly discouraged by the ECB. Valuation and reporting only.")


def entity(ccy: str) -> str:
    return f"ECB-EXR-{ccy}-EUR"


def url(*, start: str | None = None, end: str | None = None, last_n: int | None = None, currencies: list[str] | None = None) -> str:
    key = "+".join(currencies) if currencies else ""
    params = {"format": "csvdata"}
    if start:
        date.fromisoformat(start)
        params["startPeriod"] = start
    if end:
        date.fromisoformat(end)
        params["endPeriod"] = end
    if last_n:
        params["lastNObservations"] = str(int(last_n))
    return SERIES.format(ccy=key) + "?" + urlencode(params)


def parse_csv(body: bytes) -> tuple[list[dict], list[dict]]:
    """(rows, skipped). A row is {currency, date, value (Fixed), obs_status, key, title}."""
    text = body.decode("utf-8-sig")
    rows, skipped = [], []
    for r in csv.DictReader(io.StringIO(text)):
        ccy, denom, period, raw = r.get("CURRENCY"), r.get("CURRENCY_DENOM"), r.get("TIME_PERIOD"), (r.get("OBS_VALUE") or "").strip()
        if denom != "EUR" or not ccy or not period:
            skipped.append({"key": r.get("KEY"), "reason": "not a <CCY> per EUR row"})
            continue
        if (r.get("UNIT_MULT") or "0").strip() not in ("0", ""):
            skipped.append({"key": r.get("KEY"), "date": period, "reason": f"unexpected UNIT_MULT {r.get('UNIT_MULT')}"})
            continue
        if not raw or raw.lower() == "nan":
            skipped.append({"key": r.get("KEY"), "date": period, "reason": "no value published"})
            continue
        try:
            value = Fixed.parse(raw)
        except ValueError:
            skipped.append({"key": r.get("KEY"), "date": period, "reason": f"unparseable value {raw!r}"})
            continue
        rows.append({"currency": ccy, "date": date.fromisoformat(period), "value": value, "obs_status": r.get("OBS_STATUS"),
                     "key": r.get("KEY"), "title": r.get("TITLE")})
    return rows, skipped


def observations(rows: list[dict], raw_sha: str, receipt_ns: int) -> list[Observation]:
    out = []
    for r in rows:
        available, flags = capture_availability(receipt_ns, r["date"], TZ)
        status = (f"obs_status:{r['obs_status']}",) if r.get("obs_status") else ()
        out.append(Observation(
            dataset=DATASET, entity_id=entity(r["currency"]), field=FIELD, unit=f"{r['currency']}/EUR", currency=r["currency"],
            value=r["value"], period_end=r["date"].isoformat(), period_kind="DAY", receipt_time_ns=receipt_ns,
            available_time_ns=available, publication_id=f"ecb-exr:{r['currency']}:{r['date'].isoformat()}",
            source_ref=f"raw:{raw_sha}#{r['key']}/{r['date'].isoformat()}", quality=flags + status))
    return out


def ingest(store: ResearchStore, fetcher, now_ns, *, start: str | None = None, last_n: int | None = None,
           currencies: list[str] | None = None) -> dict:
    ds = dataset(load_registry(), DATASET)
    if not ds.permits("storage"):
        raise PermissionError(f"{DATASET}: storage not permitted by the registry")
    u = url(start=start, last_n=last_n, currencies=currencies)
    resp = fetcher.get(u)
    sha = store.put_raw(resp.body, provider=PROVIDER, dataset=DATASET, request={"url": resp.url},
                        receipt_time_ns=resp.receipt_time_ns, media_type=resp.content_type)
    rows, skipped = parse_csv(resp.body)
    counts = {"NEW": 0, "UNCHANGED": 0, "REVISED": 0, "CORRECTED": 0}
    for ccy in sorted({r["currency"] for r in rows}):
        title = next((r["title"] for r in rows if r["currency"] == ccy and r["title"]), f"{ccy} per EUR")
        store.upsert_entity(entity(ccy), "series", f"ECB reference rate: {title}",
                            {"provider": PROVIDER, "dataset": DATASET, "orientation": f"{ccy} per 1 EUR"})
    for o in observations(rows, sha, resp.receipt_time_ns):
        _, outcome = record_if_changed(store, o, now_ns())
        counts[outcome] = counts.get(outcome, 0) + 1
    dates = sorted({r["date"].isoformat() for r in rows})
    return {"provider": PROVIDER, "dataset": DATASET, "url": u, "raw_sha256": sha, "receipt_time": ns_to_iso(resp.receipt_time_ns),
            "currencies": len({r["currency"] for r in rows}), "rows": len(rows), "skipped": len(skipped),
            "first_date": dates[0] if dates else None, "last_date": dates[-1] if dates else None, **counts}


def currencies_stored(store) -> list[str]:
    rows = store.conn.execute("SELECT DISTINCT entity_id FROM observation WHERE dataset=?", (DATASET,)).fetchall()
    return sorted(r["entity_id"].split("-")[2] for r in rows if r["entity_id"].startswith("ECB-EXR-"))


STALE_AFTER_DAYS = 4   # calendar days behind the newest fixing in the set (covers a weekend plus a TARGET holiday)


def rates_as_of(store, as_of_ns: int, on_date: str | None = None, *, knowledge_cutoff_ns: int | None = None,
                stale_after_days: int = STALE_AFTER_DAYS) -> dict:
    """ECB reference rates available at ``as_of_ns`` for ``on_date`` (or each currency's latest).
    A currency without a fixing on the requested date (TARGET holiday, not yet published at the
    as-of time) shows its previous fixing, labelled with its own date (CAP-MAC-12). A fixing more
    than ``stale_after_days`` behind the newest fixing in the set (e.g. a discontinued or suspended
    currency) is flagged ``stale`` and listed separately; it is never silently current."""
    out, missing = {}, []
    for ccy in currencies_stored(store):
        rows = store.as_of(entity(ccy), FIELD, as_of_ns, dataset=DATASET, knowledge_cutoff_ns=knowledge_cutoff_ns)
        if on_date:
            rows = [r for r in rows if r["period_end"] <= on_date]
        if not rows:
            missing.append(ccy)
            continue
        r = rows[-1]
        out[ccy] = {"currency": ccy, "rate": r["value"], "orientation": f"{ccy} per 1 EUR", "reference_date": r["period_end"],
                    "obs_id": r["obs_id"], "available_time": ns_to_iso(r["available_time_ns"]),
                    "flags": json.loads(r["quality"] or "[]") + (["previous_fixing"] if on_date and r["period_end"] != on_date else [])}
    stale = []
    if out:
        newest = max(date.fromisoformat(v["reference_date"]) for v in out.values())
        for ccy, v in out.items():
            behind = (newest - date.fromisoformat(v["reference_date"])).days
            if behind > stale_after_days:
                v["flags"].append(f"stale:{behind}_days_behind_newest_fixing")
                stale.append(ccy)
    return {"status": "OK" if out else "UNAVAILABLE", "as_of": ns_to_iso(as_of_ns), "requested_date": on_date,
            "rates": out, "stale": stale, "unavailable": missing, "dataset": DATASET, "attribution": ATTRIBUTION, "use_note": USE_NOTE,
            **({} if out else {"reason": "no ECB reference rates available at the as-of time (not ingested or not yet published)"})}


def _input_flags(ccy: str, info: dict) -> list[str]:
    flags = [f"stale_input:{ccy}" for f in info.get("flags", []) if f.startswith("stale:")]
    if "previous_fixing" in info.get("flags", []):
        flags.append(f"previous_fixing_input:{ccy}")
    return flags


def normalize(rates: dict, base: str, scale: int = OUTPUT_SCALE) -> dict:
    """x_{c->base} (units of ``base`` per 1 unit of c) for every currency, via the EUR pivot.

    Each cross carries the reference dates of its inputs and inherits their flags: a cross built from
    two fixings of different dates is flagged ``mixed_reference_dates``; a stale or previous-day input
    is flagged ``stale_input:<CCY>`` / ``previous_fixing_input:<CCY>`` (CAP-MAC-12: never silently current)."""
    if base != "EUR" and base not in rates:
        raise LookupError(f"no ECB rate for base currency {base} at the as-of time")
    base_info = None if base == "EUR" else rates[base]
    r_base = Fixed(1) if base_info is None else Fixed.parse(base_info["rate"])
    out = {}
    for ccy, info in [("EUR", None)] + sorted(rates.items()):
        if ccy == base:
            continue
        r_c = Fixed(1) if ccy == "EUR" else Fixed.parse(info["rate"])
        x = r_base.div(r_c, scale, Rounding.HALF_EVEN)
        used = [(c, i) for c, i in ((ccy, info), (base, base_info)) if i is not None]
        dates = sorted({i["reference_date"] for _, i in used})
        flags = [f for c, i in used for f in _input_flags(c, i)]
        if len(dates) > 1:
            flags.insert(0, "mixed_reference_dates")
        out[ccy] = {"value": x.to_str(), "meaning": f"{base} per 1 {ccy}", "formula": "fx_cross_via_eur@1: R_base / R_c",
                    "inputs": [i["obs_id"] for _, i in used], "reference_dates": dates, "flags": flags,
                    "label": "derived (modified from ECB data)", "rounding": f"HALF_EVEN to {scale} dp"}
    return out


def main(argv: list[str] | None = None) -> None:
    from ...service.paths import Paths
    ap = argparse.ArgumentParser(description="Ingest ECB euro reference rates into the point-in-time store")
    sub = ap.add_subparsers(dest="cmd", required=True)
    ing = sub.add_parser("ingest")
    g = ing.add_mutually_exclusive_group(required=True)
    g.add_argument("--start", help="YYYY-MM-DD")
    g.add_argument("--last", type=int, help="last N observations per currency")
    ing.add_argument("--home", default=None)
    a = ap.parse_args(argv)
    home = Path(a.home).expanduser() if a.home else None
    paths = Paths(home) if home else Paths.default()
    research = paths.root / "research"
    research.mkdir(parents=True, exist_ok=True)
    store = ResearchStore(research / "research.sqlite")
    fetcher = make_fetcher(PROVIDER, data_config(home))
    print(json.dumps(ingest(store, fetcher, time.time_ns, start=a.start, last_n=a.last), indent=2))


if __name__ == "__main__":
    main()
