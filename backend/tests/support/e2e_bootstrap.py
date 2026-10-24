"""TEST-ONLY: register the terminal end-to-end suite's test instruments through the core socket.

Product ``unknown.cli bootstrap-sim`` registers no instrument (ADR-0020). The e2e harness
(terminal/e2e/support/harness.py ``test-instruments``) calls :func:`register_test_instruments` before it, so the
suite has the instruments its TEST quote feed double (``support.fake_market``) publishes. They are flagged
``fixture`` so every product view that shows them says they are not market data.
"""
from __future__ import annotations

import secrets
import time

from unknown.domain.fixed import Fixed
from unknown.domain.instruments import AssetClass, Instrument, TickRule
from unknown.service.ipc import CoreClient
from unknown.service.paths import Paths

from .fake_market import E2E_INSTRUMENTS

SYMBOLS = {"FIX-EQ-1": ("FIXA", "Test instrument A (e2e test double)"),
           "FIX-EQ-2": ("FIXB", "Test instrument B (e2e test double)")}


def register_test_instruments(paths: Paths | None = None) -> list[dict]:
    paths = paths or Paths.default()
    client = CoreClient(paths.socket, paths.token("api"))
    owner = {"id": "owner", "role": "OWNER", "authenticated_at_ns": time.time_ns()}
    out = []
    for iid, *_ in E2E_INSTRUMENTS:
        sym, name = SYMBOLS[iid]
        inst = Instrument(instrument_id=iid, asset_class=AssetClass.EQUITY, symbol=sym, name=name, currency="USD",
                          settlement_currency="USD", venue_mic="XNAS", quantity_increment=Fixed(1),
                          min_quantity=Fixed(1), multiplier=Fixed(1), settlement_days=1,
                          tick_rules=(TickRule(Fixed(0), Fixed.parse("0.0001")), TickRule(Fixed(1), Fixed.parse("0.01"))),
                          attributes={"fixture": True})
        r = client.command(f"e2e.register_instrument.{secrets.token_hex(8)}", "register_instrument",
                           {"instrument": inst.to_json(), "mappings": [{"provider": "simulated", "external_id": iid}]},
                           owner)
        if not r["accepted"]:
            raise SystemExit(f"register_instrument {iid} refused: {r['body']}")
        out.append({"instrument_id": iid, "accepted": True})
    return out
