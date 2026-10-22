"""IBKR contract resolution and verification: one listing to one conId, or a block (CAP-INS-04).

Rules (CAP-INS-04 behavior and acceptance criteria):

* A query is ``secType STK``, symbol, currency and primary exchange with ``exchange SMART``, or an explicit conId.
  The contract is never guessed from a name: no symbol search (reqMatchingSymbols) is used. A query without a
  primary exchange is refused locally.
* The request must match exactly one contract. Zero matches is ``UNRESOLVED``. More than one is ``AMBIGUOUS``,
  creates no mapping and blocks the listing.
* Checks on the single match:
    (a) the ISIN in SecIdList, when visible, equals the master ISIN;
    (b) PrimaryExch maps to the listing MIC;
    (c) currency matches;
    (d) LongName fuzzy-matches the issuer name;
    (e) MarketRuleIds give the price-dependent tick schedule, because MinTick reports only the smallest tick.
  Any failed check is ``CONFLICT`` and blocks routing.
* If the instrument's previous verified mapping named another conId, the result is ``CHANGED`` and blocks the
  listing until the owner approves a re-mapping. Approval creates a new mapping version, and past executions keep
  their old mapping.

Output: an ``unknown.domain.instruments.Instrument`` record *proposal* (validated by constructing it) and a
``ProviderMapping`` (provider ``ibkr``, external_id = conId, version). The owner approves; nothing here writes the
instrument master.

SDK: EClient.reqContractDetails client.py:4150, reqMarketRule :1112; EWrapper.contractDetails wrapper.py:345,
contractDetailsEnd :358, marketRule :817; ContractDetails contract.py:142 (contract, longName, minTick,
marketRuleIds, validExchanges, timeZoneId, stockType, minSize, sizeIncrement, secIdList); Contract contract.py:76;
PriceIncrement common.py:276 (lowEdge, increment); TagValue tag_value.py:28.

Status (ADR-0001): implemented and tested offline with FIXTURE contract details. NOT paper-verified. CAP-INS-04's
500-listing paper preflight (zero pacing violations) has not been run.
"""
from __future__ import annotations

import difflib
import json
import re
import sqlite3
import threading
from dataclasses import dataclass, field
from pathlib import Path

from ...domain.fixed import Fixed
from ...domain.instruments import AssetClass, Instrument, ProviderMapping, TickRule
from . import mapping as M
from .marketdata import ContractSpec

PROVIDER = "ibkr"
RESOLUTION_VERSION = "ibkr.contract_resolution@1"
NAME_MATCH_THRESHOLD = 0.6        # project choice for check (d); recorded with every result

# IBKR primaryExchange code -> ISO 10383 MIC. The CAP-MKT-02 open question remains: the ISO 10383 file has not
# been fetched, so the owner must confirm this table. An exchange outside it yields no proposal (INCOMPLETE).
IBKR_PRIMARY_TO_MIC = {"NASDAQ": "XNAS", "NYSE": "XNYS", "ARCA": "ARCX", "AMEX": "XASE", "BATS": "BATS",
                       "IEX": "IEXG"}
MIC_TABLE_SOURCE = "project table of IBKR exchange codes to ISO 10383 MICs; not verified against the ISO file"
# ContractDetails.stockType -> asset class. Anything else is not proposed (owner review).
STOCK_TYPES = {"COMMON": AssetClass.EQUITY, "ADR": AssetClass.EQUITY, "REIT": AssetClass.EQUITY,
               "ETF": AssetClass.ETF}

STATUSES = ("VERIFIED", "AMBIGUOUS", "UNRESOLVED", "CONFLICT", "CHANGED", "INCOMPLETE", "REFUSED")
_SUFFIXES = re.compile(r"\b(INCORPORATED|INC|CORPORATION|CORP|COMPANY|CO|LIMITED|LTD|PLC|HOLDINGS|HLDGS|GROUP|SA|NV|"
                       r"AG|THE|CLASS [A-Z]|CL [A-Z]|COMMON STOCK|ORDINARY SHARES|SHS)\b")


@dataclass(frozen=True)
class ListingQuery:
    """One listing to verify. ``master_isin``, ``listing_mic`` and ``issuer_name`` come from the instrument master."""
    symbol: str = ""
    currency: str = "USD"
    primary_exchange: str = ""
    con_id: int = 0
    sec_type: str = "STK"
    master_isin: str | None = None
    listing_mic: str | None = None
    issuer_name: str | None = None

    def problems(self) -> list[str]:
        out = []
        if self.sec_type != "STK":
            out.append(f"secType {self.sec_type!r} is not implemented (STK only)")
        if not self.con_id and not (self.symbol and self.primary_exchange):
            out.append("refused: a symbol needs its primary exchange (or give a conId); a contract is never "
                       "guessed from a name (CAP-INS-04)")
        if self.primary_exchange.upper() == "SMART":
            out.append("primary_exchange must be an actual exchange, not SMART")
        if len(self.currency) != 3 or not self.currency.isupper():
            out.append("currency must be an ISO 4217 code")
        return out

    def spec(self) -> ContractSpec:
        return ContractSpec(sec_type=self.sec_type, currency=self.currency, exchange="SMART", con_id=self.con_id,
                            symbol=self.symbol, primary_exchange=self.primary_exchange)

    def key(self) -> str:
        return f"{self.sec_type}:{self.symbol or '-'}:{self.primary_exchange or '-'}:{self.currency}:{self.con_id or '-'}"

    def to_json(self) -> dict:
        return {"symbol": self.symbol, "currency": self.currency, "primary_exchange": self.primary_exchange,
                "con_id": self.con_id, "sec_type": self.sec_type, "master_isin": self.master_isin,
                "listing_mic": self.listing_mic, "issuer_name": self.issuer_name}


@dataclass
class Resolution:
    status: str
    query: ListingQuery
    resolved_at_ns: int
    reasons: list = field(default_factory=list)
    checks: dict = field(default_factory=dict)
    warnings: list = field(default_factory=list)
    matches: list = field(default_factory=list)
    con_id: int | None = None
    market_rule_id: int | None = None
    tick_rules: list = field(default_factory=list)
    instrument: dict | None = None
    mapping: dict | None = None
    prior_mapping: dict | None = None
    fixture: bool = False

    @property
    def blocks_routing(self) -> bool:
        return self.status != "VERIFIED"

    def to_json(self) -> dict:
        return {"resolution_version": RESOLUTION_VERSION, "status": self.status, "blocks_routing": self.blocks_routing,
                "query": self.query.to_json(), "resolved_at_ns": self.resolved_at_ns, "reasons": list(self.reasons),
                "checks": dict(self.checks), "warnings": list(self.warnings), "matches": list(self.matches),
                "con_id": self.con_id, "market_rule_id": self.market_rule_id, "tick_rules": list(self.tick_rules),
                "instrument_proposal": self.instrument, "mapping": self.mapping, "prior_mapping": self.prior_mapping,
                "name_match_threshold": NAME_MATCH_THRESHOLD, "mic_table_source": MIC_TABLE_SOURCE,
                "fixture": self.fixture}


# ============================================================================ pure evaluation
def summarize_details(cd) -> dict:
    """Plain copy of one ContractDetails (raw evidence for the owner's contract-verification table)."""
    c = cd.contract
    sec_ids = [(getattr(t, "tag", ""), getattr(t, "value", "")) for t in (getattr(c, "secIdList", None)
                                                                         or getattr(cd, "secIdList", None) or [])]
    min_tick = getattr(cd, "minTick", None)
    return {"conId": c.conId, "symbol": c.symbol, "secType": c.secType, "exchange": c.exchange,
            "primaryExchange": c.primaryExchange, "currency": c.currency, "localSymbol": getattr(c, "localSymbol", ""),
            "tradingClass": getattr(c, "tradingClass", ""), "longName": getattr(cd, "longName", ""),
            "minTick": repr(min_tick), "marketRuleIds": getattr(cd, "marketRuleIds", ""),
            "validExchanges": getattr(cd, "validExchanges", ""), "timeZoneId": getattr(cd, "timeZoneId", ""),
            "stockType": getattr(cd, "stockType", ""), "minSize": str(getattr(cd, "minSize", "")),
            "sizeIncrement": str(getattr(cd, "sizeIncrement", "")), "secIdList": sec_ids}


def normalize_name(name: str) -> str:
    text = re.sub(r"[^A-Z0-9 ]", " ", (name or "").upper())
    text = _SUFFIXES.sub(" ", text)
    return " ".join(text.split())


def name_similarity(a: str, b: str) -> float:
    """Check (d) score in [0, 1]: 1.0 when equal after removing legal-form and share-class words, otherwise the
    ``difflib`` ratio of the normalized names.

    There is deliberately no prefix shortcut: "Apple Inc." would otherwise score 1.0 against "APPLE HOSPITALITY
    REIT INC", a different issuer, which is exactly the mismatch this check exists to catch."""
    na, nb = normalize_name(a), normalize_name(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    return difflib.SequenceMatcher(None, na, nb).ratio()


def rule_for_primary(summary: dict) -> tuple[int | None, list[int], str]:
    """MarketRuleIds is a comma list aligned with ValidExchanges; the primary exchange's rule is used."""
    ids_text = [x.strip() for x in str(summary.get("marketRuleIds") or "").split(",") if x.strip()]
    exch = [x.strip() for x in str(summary.get("validExchanges") or "").split(",") if x.strip()]
    try:
        ids = [int(x) for x in ids_text]
    except ValueError:
        return None, [], f"marketRuleIds {summary.get('marketRuleIds')!r} are not integers"
    if not ids:
        return None, [], "no marketRuleIds in contract details"
    distinct = sorted(set(ids))
    if len(distinct) == 1:
        return distinct[0], distinct, ""
    primary = summary.get("primaryExchange")
    if len(ids) == len(exch) and primary in exch:
        return ids[exch.index(primary)], distinct, ""
    return None, distinct, "market rules differ by exchange and the primary exchange's rule cannot be identified"


def tick_rules_from_increments(increments) -> list[TickRule]:
    """PriceIncrement(lowEdge, increment) floats -> TickRule(from_price, tick) via repr (ADR-0004 #5)."""
    rules = []
    for inc in sorted(increments, key=lambda p: getattr(p, "lowEdge", 0.0)):
        low = M.float_to_fixed(float(inc.lowEdge))
        tick = M.float_to_fixed(float(inc.increment))
        if low is None or tick is None:
            raise ValueError("unset lowEdge/increment in market rule")
        rules.append(TickRule(low.normalized(), tick.normalized()))
    if not rules:
        raise ValueError("market rule has no price increments")
    if not rules[0].from_price.is_zero():
        raise ValueError(f"market rule starts at {rules[0].from_price.to_str()}, not 0")
    return rules


def evaluate(query: ListingQuery, details: list, *, rules: dict, instrument_id: str, now_ns: int,
             settlement_days: int | None, prior: dict | None = None, fixture: bool = False) -> Resolution:
    """Decide one listing from contract details and market rules; pure (no I/O)."""
    res = Resolution("INCOMPLETE", query, now_ns, fixture=fixture, prior_mapping=prior)
    res.matches = [summarize_details(cd) for cd in details]
    if len(details) == 0:
        res.status = "UNRESOLVED"
        res.reasons.append("no IBKR contract matches the query")
        return res
    if len(details) > 1:
        res.status = "AMBIGUOUS"
        res.reasons.append(f"{len(details)} IBKR contracts match ({', '.join(str(m['conId']) for m in res.matches)}); "
                           "no mapping is created and the listing is blocked (CAP-INS-04)")
        return res
    m = res.matches[0]
    cd = details[0]
    res.con_id = int(m["conId"])
    conflicts = []
    # secType / conId
    if m["secType"] != query.sec_type:
        conflicts.append(f"secType {m['secType']!r} != {query.sec_type!r}")
    if query.con_id and res.con_id != query.con_id:
        conflicts.append(f"conId {res.con_id} != requested {query.con_id}")
    # (c) currency
    res.checks["currency"] = "pass" if m["currency"] == query.currency else "fail"
    if res.checks["currency"] == "fail":
        conflicts.append(f"currency {m['currency']!r} != {query.currency!r}")
    # (b) primary exchange -> MIC
    mic = IBKR_PRIMARY_TO_MIC.get(m["primaryExchange"])
    if query.primary_exchange and m["primaryExchange"] != query.primary_exchange:
        conflicts.append(f"primaryExchange {m['primaryExchange']!r} != {query.primary_exchange!r}")
    if query.listing_mic:
        if mic is None:
            res.checks["primary_exchange_mic"] = "unmapped"
        else:
            res.checks["primary_exchange_mic"] = "pass" if mic == query.listing_mic else "fail"
            if mic != query.listing_mic:
                conflicts.append(f"primary exchange {m['primaryExchange']} maps to {mic}, listing MIC is "
                                 f"{query.listing_mic}")
    else:
        res.checks["primary_exchange_mic"] = "skipped (no listing MIC in the query)"
    # (a) ISIN
    isins = [v for t, v in m["secIdList"] if str(t).upper() == "ISIN"]
    if query.master_isin:
        if not isins:
            res.checks["isin"] = "not visible in SecIdList"
        elif query.master_isin in isins:
            res.checks["isin"] = "pass"
        else:
            res.checks["isin"] = "fail"
            conflicts.append(f"SecIdList ISIN {isins} != master ISIN {query.master_isin}")
    else:
        res.checks["isin"] = "skipped (no master ISIN)"
    # (d) name
    if query.issuer_name:
        score = name_similarity(m["longName"], query.issuer_name)
        res.checks["issuer_name"] = f"{'pass' if score >= NAME_MATCH_THRESHOLD else 'fail'} ({score:.2f})"
        if score < NAME_MATCH_THRESHOLD:
            conflicts.append(f"LongName {m['longName']!r} does not match issuer {query.issuer_name!r} "
                             f"(similarity {score:.2f} < {NAME_MATCH_THRESHOLD})")
    else:
        res.checks["issuer_name"] = "skipped (no issuer name)"
    if conflicts:
        res.status = "CONFLICT"
        res.reasons += conflicts
        return res
    # changed mapping
    if prior is not None and str(prior.get("external_id")) != str(res.con_id):
        res.status = "CHANGED"
        res.reasons.append(f"instrument {instrument_id} was mapped to conId {prior.get('external_id')} (version "
                           f"{prior.get('version')}); IBKR now returns {res.con_id}. Blocked until the owner approves a "
                           "re-mapping, which creates a new version")
        return res
    # (e) tick schedule
    rule_id, distinct, why = rule_for_primary(m)
    res.market_rule_id = rule_id
    if len(distinct) > 1:
        res.warnings.append(f"market rules differ by exchange: {distinct}; the primary exchange's rule is used")
    if rule_id is None:
        res.reasons.append(why)
        return res
    if rule_id not in rules:
        res.reasons.append(f"market rule {rule_id} was not received (reqMarketRule)")
        return res
    try:
        tick_rules = tick_rules_from_increments(rules[rule_id])
    except ValueError as exc:
        res.reasons.append(f"market rule {rule_id}: {exc}")
        return res
    res.tick_rules = [{"from_price": r.from_price.to_str(), "tick": r.tick.to_str()} for r in tick_rules]
    res.checks["tick_schedule"] = f"market rule {rule_id}: {len(tick_rules)} band(s)"
    min_tick = M.float_to_fixed(cd.minTick) if isinstance(getattr(cd, "minTick", None), float) else None
    smallest = min(r.tick for r in tick_rules)
    if min_tick is not None and min_tick != smallest:
        res.warnings.append(f"MinTick {min_tick.to_str()} != smallest market-rule increment {smallest.to_str()}")
    # proposal
    asset_class = STOCK_TYPES.get(m["stockType"])
    if mic is None:
        res.reasons.append(f"primary exchange {m['primaryExchange']!r} has no MIC in the project table; owner review")
        return res
    if asset_class is None:
        res.reasons.append(f"stockType {m['stockType']!r} is not mapped to an asset class; owner review")
        return res
    if settlement_days is None:
        res.reasons.append("settlement_days not supplied: IBKR contract details do not state the settlement cycle "
                           "(take it from the listing master / owner policy)")
        return res
    version = int(prior["version"]) if prior is not None else 1
    try:
        instrument = Instrument(
            instrument_id=instrument_id, asset_class=asset_class, symbol=m["symbol"], name=m["longName"] or m["symbol"],
            currency=m["currency"], settlement_currency=m["currency"], venue_mic=mic, quantity_increment=Fixed(1),
            min_quantity=Fixed(1), multiplier=Fixed(1), settlement_days=settlement_days, tick_rules=tuple(tick_rules),
            attributes={"ibkr.conId": res.con_id, "ibkr.primaryExchange": m["primaryExchange"],
                        "ibkr.stockType": m["stockType"], "ibkr.timeZoneId": m["timeZoneId"],
                        "ibkr.marketRuleId": rule_id, "ibkr.minSize": m["minSize"],
                        "ibkr.sizeIncrement": m["sizeIncrement"], "ibkr.validExchanges": m["validExchanges"],
                        "proposal.source": RESOLUTION_VERSION,
                        "proposal.owner_review": [
                            "leveraged/inverse flags are not provided by IBKR and default to false",
                            "quantity increment 1 share: fractional quantities are disabled in this release"]})
    except ValueError as exc:
        res.reasons.append(f"instrument proposal invalid: {exc}")
        return res
    mapping = ProviderMapping(provider=PROVIDER, external_id=str(res.con_id), instrument_id=instrument_id,
                              version=version, verified_at_ns=now_ns,
                              details={"primaryExchange": m["primaryExchange"], "currency": m["currency"],
                                       "symbol": m["symbol"], "marketRuleId": rule_id, "checks": dict(res.checks)})
    res.instrument = instrument.to_json()
    res.mapping = {"provider": mapping.provider, "external_id": mapping.external_id,
                   "instrument_id": mapping.instrument_id, "version": mapping.version,
                   "verified_at_ns": mapping.verified_at_ns, "details": mapping.details}
    res.status = "VERIFIED"
    return res


# ============================================================================ store
CONTRACT_SCHEMA = r"""
CREATE TABLE IF NOT EXISTS ibkr_contract_resolution (
  resolution_id INTEGER PRIMARY KEY, query_key TEXT NOT NULL, instrument_id TEXT NOT NULL, status TEXT NOT NULL,
  con_id INTEGER, mapping_version INTEGER, resolved_at_ns INTEGER NOT NULL, record TEXT NOT NULL, fixture INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ibkr_contract_by_instrument ON ibkr_contract_resolution(instrument_id, resolved_at_ns);
"""


class ContractStore:
    """Append-only history of resolutions (contract reference data, not exchange market data)."""

    def __init__(self, path: str | Path):
        self.conn = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        if str(path) != ":memory:":
            self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(CONTRACT_SCHEMA)
        self._lock = threading.Lock()

    def close(self) -> None:
        self.conn.close()

    def record(self, instrument_id: str, res: Resolution) -> int:
        with self._lock:
            c = self.conn.execute(
                "INSERT INTO ibkr_contract_resolution(query_key, instrument_id, status, con_id, mapping_version, "
                "resolved_at_ns, record, fixture) VALUES (?,?,?,?,?,?,?,?)",
                (res.query.key(), instrument_id, res.status, res.con_id,
                 res.mapping["version"] if res.mapping else None, res.resolved_at_ns,
                 json.dumps(res.to_json(), sort_keys=True), int(res.fixture)))
            return c.lastrowid

    def current_mapping(self, instrument_id: str) -> dict | None:
        """The latest VERIFIED mapping, or an owner-approved one (``approve_remap``)."""
        with self._lock:
            row = self.conn.execute("SELECT record FROM ibkr_contract_resolution WHERE instrument_id=? AND status IN "
                                    "('VERIFIED', 'APPROVED_REMAP') ORDER BY resolution_id DESC LIMIT 1",
                                    (instrument_id,)).fetchone()
        return json.loads(row["record"])["mapping"] if row else None

    def approve_remap(self, instrument_id: str, con_id: int, *, by: str, note: str, now_ns: int) -> dict:
        """Owner approval after a CHANGED result: a new mapping version; earlier versions are kept.

        The row inherits the instrument's FIXTURE flag: a remap of a fixture listing is still fixture data and must
        stay hidden and labelled in the verification table."""
        prior = self.current_mapping(instrument_id)
        version = (int(prior["version"]) + 1) if prior else 1
        with self._lock:
            row = self.conn.execute("SELECT MAX(fixture) AS f FROM ibkr_contract_resolution WHERE instrument_id=?",
                                    (instrument_id,)).fetchone()
        fixture = int(row["f"] or 0) if row is not None else 0
        mapping = {"provider": PROVIDER, "external_id": str(con_id), "instrument_id": instrument_id,
                   "version": version, "verified_at_ns": None,
                   "details": {"approved_by": by, "note": note, "approved_at_ns": now_ns,
                               "previous": prior["external_id"] if prior else None}}
        record = {"resolution_version": RESOLUTION_VERSION, "status": "APPROVED_REMAP", "mapping": mapping,
                  "blocks_routing": True, "reasons": ["re-verify at the next preflight before routing"],
                  "fixture": bool(fixture)}
        with self._lock:
            self.conn.execute(
                "INSERT INTO ibkr_contract_resolution(query_key, instrument_id, status, con_id, mapping_version, "
                "resolved_at_ns, record, fixture) VALUES (?,?,?,?,?,?,?,?)",
                (f"remap:{instrument_id}", instrument_id, "APPROVED_REMAP", con_id, version, now_ns,
                 json.dumps(record, sort_keys=True), fixture))
        return mapping

    def latest(self, limit: int = 500) -> list[dict]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT r.* FROM ibkr_contract_resolution r JOIN (SELECT instrument_id, MAX(resolution_id) AS mx FROM "
                "ibkr_contract_resolution GROUP BY instrument_id) l ON r.resolution_id = l.mx ORDER BY r.instrument_id "
                "LIMIT ?", (int(limit),)).fetchall()
        return [dict(r) for r in rows]


# ============================================================================ resolver (talks to IBKR)
class _DetailsHandler:
    def __init__(self):
        self.details: list = []
        self.end = False
        self.error: tuple[int, str] | None = None

    def on_contract_details(self, cd) -> None:
        self.details.append(cd)

    def on_contract_details_end(self) -> None:
        self.end = True

    def on_error(self, code: int, text: str) -> None:
        if 2100 <= code < 2200:
            return          # TWS warning range: never a failure of the request
        self.error = (code, text)


class ContractResolver:
    """reqContractDetails + reqMarketRule through an ``IbkrMarketDataSession`` (paced as 'query' requests)."""

    def __init__(self, session, *, store: ContractStore | None = None, timeout_s: float | None = None):
        self._session = session
        self._store = store
        self._timeout = timeout_s
        self._rules: dict[int, list] = {}
        self._rules_lock = threading.Lock()
        session.add_market_rule_listener(self._on_rule)

    def _on_rule(self, rule_id: int, increments: list) -> None:
        copied = [type("PriceIncrementCopy", (), {"lowEdge": float(p.lowEdge), "increment": float(p.increment)})()
                  for p in increments]
        with self._rules_lock:
            self._rules[rule_id] = copied

    def cached_rules(self) -> dict:
        with self._rules_lock:
            return dict(self._rules)

    def resolve(self, query: ListingQuery, *, instrument_id: str, settlement_days: int | None,
                fixture: bool = False) -> Resolution:
        now = self._session.clock.now_ns()
        problems = query.problems()
        if problems:
            res = Resolution("REFUSED", query, now, reasons=problems, fixture=fixture)
            return self._record(instrument_id, res)
        timeout = self._timeout if self._timeout is not None else self._session.config.request_timeout_s
        handler = _DetailsHandler()
        req_id = self._session.new_req_id()
        self._session.register(req_id, handler)
        try:
            err = self._session.request("query", "reqContractDetails", req_id,
                                        query.spec().to_sdk(self._session.sdk.Contract))
            if err:
                return self._record(instrument_id, Resolution("INCOMPLETE", query, now, reasons=[err], fixture=fixture))
            self._session.wait(lambda: handler.end or handler.error is not None, timeout)
        finally:
            self._session.unregister(req_id)
        if handler.error is not None and not handler.end:
            code, text = handler.error
            status = "UNRESOLVED" if code == 200 else "INCOMPLETE"
            # IBKR 200 also covers "The contract description specified for <Symbol> is ambiguous."
            if code == 200 and "ambiguous" in text.lower():
                status = "AMBIGUOUS"
            return self._record(instrument_id, Resolution(status, query, now, reasons=[f"IBKR {code}: {text}"],
                                                          fixture=fixture))
        if not handler.end:
            return self._record(instrument_id, Resolution("INCOMPLETE", query, now, fixture=fixture,
                                                          reasons=["contractDetailsEnd not received within the "
                                                                   "timeout (large requests can be held ~1 min)"]))
        if len(handler.details) == 1:
            rule_id, distinct, _ = rule_for_primary(summarize_details(handler.details[0]))
            for rid in ([rule_id] if rule_id is not None else []):
                if rid in self.cached_rules():
                    continue
                if self._session.request("query", "reqMarketRule", rid) is None:
                    self._session.wait(lambda rid=rid: rid in self.cached_rules(), timeout)
        prior = self._store.current_mapping(instrument_id) if self._store is not None else None
        res = evaluate(query, handler.details, rules=self.cached_rules(), instrument_id=instrument_id,
                       now_ns=self._session.clock.now_ns(), settlement_days=settlement_days, prior=prior,
                       fixture=fixture)
        return self._record(instrument_id, res)

    def _record(self, instrument_id: str, res: Resolution) -> Resolution:
        if self._store is not None:
            self._store.record(instrument_id, res)
        return res
