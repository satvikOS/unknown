"""Test-only seeding for the research-side end-to-end flows. Never used outside `npm run test:e2e`.

  python seed_research.py --home <UNKNOWN_HOME> [--sessions N] [--core-replay-sessions N]

Writes, under <home>/research/ (the files the API opens read-only per request):

  research.sqlite     the point-in-time store:
                      - the SYNTHETIC issuers of backend/tests/test_company_fixtures.py (Fixture Widgets Inc and its
                        peers), ingested through the real ``unknown.data.sec.ingest`` path with a fake opener;
                      - a FIXTURE Treasury par curve on two dates (the second without the 1.5M tenor) and FIXTURE ECB
                        reference rates, both through the real ``treasury.ingest`` / ``ecb.ingest`` parsers with a stub
                        fetcher;
  experiments.sqlite  the research registry, populated by the fixture study (``unknown.research.study``).

Every value is synthetic and labelled FIXTURE. Prints one JSON line with what it wrote."""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[3] / "backend"
sys.path.insert(0, str(BACKEND / "src"))
sys.path.insert(0, str(BACKEND / "tests"))

from test_company_fixtures import ISSUERS, ingest_issuer  # noqa: E402  (SYNTHETIC SEC fixtures)

from unknown.data.http import Response  # noqa: E402
from unknown.data.pit import ResearchStore  # noqa: E402
from unknown.data.sources import ecb, treasury  # noqa: E402
from unknown.research.study import main as study_main  # noqa: E402

# FIXTURE par yields (percent), synthetic: an inverted front end and a positive 10Y-2Y slope.
CURVE_1 = {"BC_1MONTH": "4.31", "BC_1_5MONTH": "4.30", "BC_2MONTH": "4.28", "BC_3MONTH": "4.24", "BC_4MONTH": "4.20",
           "BC_6MONTH": "4.11", "BC_1YEAR": "3.98", "BC_2YEAR": "3.86", "BC_3YEAR": "3.84", "BC_5YEAR": "3.92",
           "BC_7YEAR": "4.05", "BC_10YEAR": "4.21", "BC_20YEAR": "4.58", "BC_30YEAR": "4.62"}
CURVE_2 = {**{k: v for k, v in CURVE_1.items() if k != "BC_1_5MONTH"}, "BC_10YEAR": "4.24", "BC_2YEAR": "3.88"}
CURVE_DATES = ("2026-10-20", "2026-10-21")
ECB_RATES = {"2026-10-20": {"USD": "1.1612", "JPY": "176.42", "GBP": "0.86725", "CHF": "0.9318"},
             "2026-10-21": {"USD": "1.1634", "JPY": "176.90", "GBP": "0.86810", "CHF": "0.9302"}}


def _ns(text: str) -> int:
    return int(datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()) * 1_000_000_000


class _Stub:
    def __init__(self, body: bytes, media: str, receipt_ns: int):
        self.body, self.media, self.receipt_ns = body, media, receipt_ns

    def get(self, url):
        return Response(url, 200, self.body, self.media, self.receipt_ns)


def _treasury_feed() -> bytes:
    def entry(day: str, values: dict) -> str:
        fields = [f'<d:{f} m:type="Edm.Double" m:null="true" />' if values.get(f) is None else
                  f'<d:{f} m:type="Edm.Double">{values[f]}</d:{f}>' for f in treasury.TENORS]
        return (f'<entry><id>fixture</id><content type="application/xml"><m:properties><d:NEW_DATE m:type="Edm.DateTime">'
                f'{day}T00:00:00</d:NEW_DATE>{"".join(fields)}</m:properties></content></entry>')
    return ('<?xml version="1.0" encoding="utf-8" standalone="yes"?><!-- FIXTURE (synthetic values) -->'
            '<feed xmlns="http://www.w3.org/2005/Atom" xmlns:d="http://schemas.microsoft.com/ado/2007/08/dataservices" '
            'xmlns:m="http://schemas.microsoft.com/ado/2007/08/dataservices/metadata"><title type="text">FIXTURE</title>'
            + entry(CURVE_DATES[0], CURVE_1) + entry(CURVE_DATES[1], CURVE_2) + "</feed>").encode()


def _ecb_csv() -> bytes:
    header = "KEY,FREQ,CURRENCY,CURRENCY_DENOM,EXR_TYPE,EXR_SUFFIX,TIME_PERIOD,OBS_VALUE,OBS_STATUS,UNIT_MULT,DECIMALS,TITLE"
    rows = [f'EXR.D.{c}.EUR.SP00.A,D,{c},EUR,SP00,A,{d},{v},A,0,4,"FIXTURE {c}/EUR"'
            for d, rates in ECB_RATES.items() for c, v in rates.items()]
    return "\n".join([header, *rows]).encode()


def seed(home: Path, sessions: int, core_replay_sessions: int) -> dict:
    research = home / "research"
    research.mkdir(parents=True, exist_ok=True)
    store = ResearchStore(research / "research.sqlite")
    try:
        for cik, name, sic, scale, ni, ticker in ISSUERS:
            ingest_issuer(store, cik, name, sic, scale, ni, ticker)
        t_receipt = _ns("2026-10-21T22:30:00Z")        # 18:30 New York on the second curve date (a live capture)
        tr = treasury.ingest(store, _Stub(_treasury_feed(), "application/xml", t_receipt), lambda: t_receipt, month="202610")
        e_receipt = _ns("2026-10-21T14:05:00Z")        # 16:05 CEST on the second reference date
        ec = ecb.ingest(store, _Stub(_ecb_csv(), "text/csv", e_receipt), lambda: e_receipt, last_n=2)
    finally:
        store.close()
    out = research / "study-out"
    study_main(["--registry", str(research / "experiments.sqlite"), "--out", str(out), "--sessions", str(sessions),
                "--core-replay-sessions", str(core_replay_sessions)])
    return {"issuers": [i[0] for i in ISSUERS], "treasury": {k: tr[k] for k in ("curve_dates", "first_date", "last_date", "NEW")},
            "ecb": {k: ec[k] for k in ("currencies", "rows", "first_date", "last_date")},
            "registry": str(research / "experiments.sqlite")}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--home", required=True)
    ap.add_argument("--sessions", type=int, default=750)
    ap.add_argument("--core-replay-sessions", type=int, default=130)
    a = ap.parse_args()
    print(json.dumps(seed(Path(a.home), a.sessions, a.core_replay_sessions)), flush=True)


if __name__ == "__main__":
    main()
