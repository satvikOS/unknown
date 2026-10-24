"""Static definitions of the analyst's read-only tools (names, descriptions, input schemas).

Imported by the API (to fix the job envelope's allowed-tool list at submission) and by the worker (to build the
provider request and the tool registry). No data access here: the handlers live in ``sources.py``.
"""
from __future__ import annotations

PLATFORM_DATASET = "platform.own"     # content written by the platform itself (unavailable reasons, register records)

_CIK = {"type": "string", "minLength": 1, "maxLength": 13, "description": "SEC CIK, digits only (from company_search)"}

TOOL_DEFS: tuple[dict, ...] = (
    {"name": "company_search",
     "description": "Find an issuer by ticker, name or CIK in the research store. Returns candidates with CIK, entity id, "
                    "name and listings. Use it first to turn a ticker or a linked security into a CIK.",
     "input_schema": {"type": "object", "additionalProperties": False, "required": ["query"],
                      "properties": {"query": {"type": "string", "minLength": 1, "maxLength": 100},
                                     "limit": {"type": "integer", "minimum": 1, "maximum": 10}}}},
    {"name": "company_filings",
     "description": "List an issuer's SEC filings accepted so far (newest first) from the stored SEC submissions: form, "
                    "accession, filing and report dates, 8-K item numbers, primary document description and SEC links. "
                    "Filing full text is not stored; say so when it is needed.",
     "input_schema": {"type": "object", "additionalProperties": False, "required": ["cik"],
                      "properties": {"cik": _CIK,
                                     "forms": {"type": "array", "maxItems": 10,
                                               "items": {"type": "string", "minLength": 1, "maxLength": 12}},
                                     "limit": {"type": "integer", "minimum": 1, "maximum": 40}}}},
    {"name": "company_financials",
     "description": "Standardised financial statements (income, balance sheet, cash flow, per share, shares) from "
                    "point-in-time XBRL company facts: basis FY, Q or TTM, the latest N periods. Each value names its "
                    "XBRL concept, the filing form and accession it came from, or a reason it is missing.",
     "input_schema": {"type": "object", "additionalProperties": False, "required": ["cik"],
                      "properties": {"cik": _CIK, "basis": {"type": "string", "enum": ["FY", "Q", "TTM"]},
                                     "periods": {"type": "integer", "minimum": 1, "maximum": 8}}}},
    {"name": "news_search",
     "description": "Search the news and filings store (SEC EDGAR filings feed, SEC, Federal Reserve and other "
                    "official releases) by full-text query, issuer CIK and/or form. Returns items with id, source, "
                    "title, form, accession, 8-K items, dates and a snippet; use news_item for one item's full record.",
     "input_schema": {"type": "object", "additionalProperties": False,
                      "properties": {"query": {"type": "string", "minLength": 1, "maxLength": 200}, "cik": _CIK,
                                     "forms": {"type": "array", "maxItems": 10,
                                               "items": {"type": "string", "minLength": 1, "maxLength": 12}},
                                     "limit": {"type": "integer", "minimum": 1, "maximum": 30}}}},
    {"name": "news_item",
     "description": "One news or filing item by id: title, summary text as published by the source, form, accession, "
                    "8-K items, links, publication and receipt times, and syndicated copies.",
     "input_schema": {"type": "object", "additionalProperties": False, "required": ["item_id"],
                      "properties": {"item_id": {"type": "integer", "minimum": 1}}}},
    {"name": "market_snapshot",
     "description": "Latest quote for an instrument (bid, ask, last). Returns the reason when quotes cannot be given.",
     "input_schema": {"type": "object", "additionalProperties": False, "required": ["instrument"],
                      "properties": {"instrument": {"type": "string", "minLength": 1, "maxLength": 40}}}},
    {"name": "market_bars",
     "description": "Stored historical price bars for an instrument (point in time, newest last). Returns the reason "
                    "when no bars are stored or their licence does not allow model processing.",
     "input_schema": {"type": "object", "additionalProperties": False, "required": ["instrument"],
                      "properties": {"instrument": {"type": "string", "minLength": 1, "maxLength": 40},
                                     "bar_size": {"type": "string", "minLength": 1, "maxLength": 16},
                                     "limit": {"type": "integer", "minimum": 1, "maximum": 300}}}},
    {"name": "macro_overview",
     "description": "Cross-country macro comparison (GDP growth, inflation, unemployment, policy rates) from the "
                    "World Bank, OECD, BIS and FRED series held, or the reason they cannot be given.",
     "input_schema": {"type": "object", "additionalProperties": False,
                      "properties": {"countries": {"type": "array", "maxItems": 12,
                                                   "items": {"type": "string", "minLength": 2, "maxLength": 2}}}}},
    {"name": "us_yield_curve",
     "description": "US Treasury par yield curve as of a date (default latest), or the reason it cannot be given.",
     "input_schema": {"type": "object", "additionalProperties": False,
                      "properties": {"date": {"type": "string", "minLength": 10, "maxLength": 10}}}},
    {"name": "portfolio_positions",
     "description": "The owner's open positions (quantity and cost per instrument) and cash balances, read-only, or "
                    "the reason they cannot be given.",
     "input_schema": {"type": "object", "additionalProperties": False, "properties": {}}},
    {"name": "data_capabilities",
     "description": "What the platform can and cannot provide: capability records (id, title, status, gaps) matching a "
                    "search text, and the meaning of unavailable-reason codes. Use it to explain missing data.",
     "input_schema": {"type": "object", "additionalProperties": False,
                      "properties": {"search": {"type": "string", "minLength": 1, "maxLength": 60}}}},
)

TOOL_NAMES: tuple[str, ...] = tuple(t["name"] for t in TOOL_DEFS)
