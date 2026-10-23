"""Feed parsers (standard library only). Nothing here touches the network or a database.

``parse_edgar_atom`` reads the EDGAR Latest Filings Atom feed (``browse-edgar?action=getcurrent&output=atom``). One
entry is one (filing, entity role) pair, titled ``<form> - <name> (<10-digit CIK>) (<role>)``; its ``<updated>`` is
the EDGAR acceptance time, its ``<id>`` carries the accession number, its alternate link is the filing index, and an
8-K summary lists ``Item N.NN: <title>`` lines. Entries of one accession are merged into one filing with several
entities.

``parse_feed`` reads RSS 2.0 and Atom 1.0. It keeps what the feed provides: headline, link, guid, time, category and
a summary cut to ``SUMMARY_MAX`` plain-text characters (some feeds put a whole release in the description; that text
is not republished). A time the feed does not give stays unknown; a date without a time keeps day precision.

Documents carrying a DOCTYPE or ENTITY declaration are refused (no entity expansion from a remote document).
"""
from __future__ import annotations

import html
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from zoneinfo import ZoneInfo

SUMMARY_MAX = 600
ATOM = "{http://www.w3.org/2005/Atom}"

# Form 8-K items (SEC Form 8-K General Instructions B and the item list of the form; public domain).
EIGHT_K_ITEMS: dict[str, tuple[str, str]] = {
    "1.01": ("material_agreement", "Entry into a Material Definitive Agreement"),
    "1.02": ("agreement_terminated", "Termination of a Material Definitive Agreement"),
    "1.03": ("bankruptcy", "Bankruptcy or Receivership"),
    "1.04": ("mine_safety", "Mine Safety - Reporting of Shutdowns and Patterns of Violations"),
    "1.05": ("cybersecurity_incident", "Material Cybersecurity Incidents"),
    "2.01": ("acquisition_or_disposition", "Completion of Acquisition or Disposition of Assets"),
    "2.02": ("results", "Results of Operations and Financial Condition"),
    "2.03": ("financial_obligation", "Creation of a Direct Financial Obligation"),
    "2.04": ("obligation_triggered", "Triggering Events That Accelerate or Increase a Direct Financial Obligation"),
    "2.05": ("exit_costs", "Costs Associated with Exit or Disposal Activities"),
    "2.06": ("impairment", "Material Impairments"),
    "3.01": ("delisting_notice", "Notice of Delisting or Failure to Satisfy a Continued Listing Rule; Transfer of Listing"),
    "3.02": ("unregistered_equity_sale", "Unregistered Sales of Equity Securities"),
    "3.03": ("holder_rights_modified", "Material Modification to Rights of Security Holders"),
    "4.01": ("auditor_change", "Changes in Registrant's Certifying Accountant"),
    "4.02": ("non_reliance", "Non-Reliance on Previously Issued Financial Statements"),
    "5.01": ("change_in_control", "Changes in Control of Registrant"),
    "5.02": ("officer_director_change", "Departure or Appointment of Directors or Certain Officers"),
    "5.03": ("charter_bylaw_amendment", "Amendments to Articles of Incorporation or Bylaws; Change in Fiscal Year"),
    "5.04": ("benefit_plan_suspension", "Temporary Suspension of Trading Under Registrant's Employee Benefit Plans"),
    "5.05": ("ethics_code", "Amendment to or Waiver of the Code of Ethics"),
    "5.06": ("shell_status", "Change in Shell Company Status"),
    "5.07": ("shareholder_vote", "Submission of Matters to a Vote of Security Holders"),
    "5.08": ("director_nominations", "Shareholder Director Nominations"),
    "6.01": ("abs_material", "ABS Informational and Computational Material"),
    "6.02": ("abs_servicer_change", "Change of Servicer or Trustee"),
    "6.03": ("abs_credit_enhancement", "Change in Credit Enhancement or Other External Support"),
    "6.04": ("abs_distribution_failure", "Failure to Make a Required Distribution"),
    "6.05": ("abs_securities_act_update", "Securities Act Updating Disclosure"),
    "6.06": ("abs_static_pool", "Static Pool"),
    "7.01": ("reg_fd", "Regulation FD Disclosure"),
    "8.01": ("other_events", "Other Events"),
    "9.01": ("financial_statements_exhibits", "Financial Statements and Exhibits"),
}


class FeedError(ValueError):
    pass


@dataclass
class FeedItem:
    guid: str
    title: str
    link: str | None
    summary: str | None = None
    summary_truncated: bool = False
    published_ns: int | None = None
    published_precision: str | None = None      # 'second' | 'day' | None (the feed gave no usable time)
    category: str | None = None
    time_zone_assumed: str | None = None   # the feed gave a time without a zone; the source's declared zone was used
    # EDGAR filings only
    form: str | None = None
    accession: str | None = None
    items: list[dict] = field(default_factory=list)
    entities: list[dict] = field(default_factory=list)   # {cik, name, role}
    filed_date: str | None = None
    size: str | None = None


def _root(body: bytes) -> ET.Element:
    head = body[:4096].upper()
    if b"<!DOCTYPE" in head or b"<!ENTITY" in body.upper():
        raise FeedError("document declares a DOCTYPE or ENTITY; refused")
    try:
        return ET.fromstring(body)
    except ET.ParseError as exc:
        raise FeedError(f"not well-formed XML: {exc}") from None


def clean_text(value: str | None) -> str:
    """HTML to plain text: tags dropped, entities decoded (twice for feeds that escape their HTML), spaces collapsed."""
    if not value:
        return ""
    t = html.unescape(value)
    t = re.sub(r"<(script|style)\b.*?</\1>", " ", t, flags=re.S | re.I)
    t = re.sub(r"<br\s*/?>|</p>|</tr>|</li>", " ", t, flags=re.I)
    t = re.sub(r"<[^>]+>", " ", t)
    t = html.unescape(t)
    return re.sub(r"\s+", " ", t).strip()


def summarize(value: str | None, title: str) -> tuple[str | None, bool]:
    text = clean_text(value)
    if not text or text.casefold() == title.casefold():
        return None, False
    if len(text) <= SUMMARY_MAX:
        return text, False
    cut = text[:SUMMARY_MAX]
    space = cut.rfind(" ")
    return (cut[:space] if space > SUMMARY_MAX // 2 else cut).rstrip(" ,;:") + "…", True


_DAY = re.compile(r"^(\d{1,2})\s+([A-Za-z]{3})[a-z]*,?\s+(\d{4})(?:\s+([+-])(\d{2}):?(\d{2}))?$")
_MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def parse_time(value: str | None, default_tz: str | None = None) -> tuple[int | None, str | None]:
    """(epoch ns, precision) from RFC 822, ISO 8601 or a 'DD Mon, YYYY [+HHMM]' date; (None, None) if unusable.
    A timestamp without a zone is refused rather than guessed, unless the caller passes the zone the source
    publishes in (``default_tz``, an IANA name), which the caller then flags on the item."""
    v = (value or "").strip()
    if not v:
        return None, None
    m = _DAY.match(v)
    if m and m.group(2).lower() in _MONTHS:
        off = timedelta(0)
        if m.group(4):
            off = timedelta(hours=int(m.group(5)), minutes=int(m.group(6))) * (1 if m.group(4) == "+" else -1)
        d = datetime(int(m.group(3)), _MONTHS[m.group(2).lower()], int(m.group(1)), tzinfo=timezone(off))
        return int(d.timestamp()) * 1_000_000_000, "day"
    d = None
    try:
        d = parsedate_to_datetime(v)
    except (TypeError, ValueError, IndexError):
        try:
            d = datetime.fromisoformat(v.replace("Z", "+00:00"))
        except ValueError:
            return None, None
    if d is not None and d.tzinfo is None and default_tz:
        d = d.replace(tzinfo=ZoneInfo(default_tz))
    if d is None or d.tzinfo is None:
        return None, None
    return int(d.timestamp() * 1_000_000) * 1000, "second"


def _text(el: ET.Element | None, tag: str) -> str | None:
    if el is None:
        return None
    child = el.find(tag)
    return child.text if child is not None and child.text is not None else None


def _atom_link(entry: ET.Element) -> str | None:
    links = entry.findall(f"{ATOM}link")
    for ln in links:
        if ln.get("rel", "alternate") == "alternate" and ln.get("href"):
            return ln.get("href")
    return links[0].get("href") if links and links[0].get("href") else None


def _time(value: str | None, default_tz: str | None) -> tuple[int | None, str | None, str | None]:
    ts, prec = parse_time(value)
    if ts is None and default_tz:
        ts, prec = parse_time(value, default_tz)
        return ts, prec, default_tz if ts is not None else None
    return ts, prec, None


def parse_feed(body: bytes, default_tz: str | None = None) -> list[FeedItem]:
    root = _root(body)
    out: list[FeedItem] = []
    if root.tag == f"{ATOM}feed":
        for e in root.findall(f"{ATOM}entry"):
            title = clean_text(_text(e, f"{ATOM}title"))
            link = _atom_link(e)
            guid = (_text(e, f"{ATOM}id") or link or "").strip()
            if not title or not guid:
                continue
            ts, prec, assumed = _time(_text(e, f"{ATOM}published") or _text(e, f"{ATOM}updated"), default_tz)
            summary, cut = summarize(_text(e, f"{ATOM}summary") or _text(e, f"{ATOM}content"), title)
            cat = e.find(f"{ATOM}category")
            out.append(FeedItem(guid=guid, title=title, link=link, summary=summary, summary_truncated=cut, published_ns=ts,
                                published_precision=prec, category=cat.get("term") if cat is not None else None,
                                time_zone_assumed=assumed))
        return out
    channel = root.find("channel")
    if root.tag != "rss" or channel is None:
        raise FeedError(f"not an RSS 2.0 or Atom document (root <{root.tag}>)")
    for it in channel.findall("item"):
        title = clean_text(_text(it, "title"))
        link = (_text(it, "link") or "").strip() or None
        guid = (_text(it, "guid") or link or "").strip()
        if not title or not guid:
            continue
        ts, prec, assumed = _time(_text(it, "pubDate") or _text(it, "{http://purl.org/dc/elements/1.1/}date"), default_tz)
        summary, cut = summarize(_text(it, "description"), title)
        out.append(FeedItem(guid=guid, title=title, link=link, summary=summary, summary_truncated=cut, published_ns=ts,
                            published_precision=prec, category=clean_text(_text(it, "category")) or None,
                            time_zone_assumed=assumed))
    return out


_EDGAR_TITLE = re.compile(r"^(?P<form>.+?) - (?P<name>.+) \((?P<cik>\d{10})\) \((?P<role>[^()]+)\)$")
_ACCESSION = re.compile(r"\b(\d{10}-\d{2}-\d{6})\b")
_ITEM = re.compile(r"Item\s+(\d{1,2}\.\d{2})\s*:\s*([^<\n]*)")


def parse_edgar_atom(body: bytes) -> list[FeedItem]:
    """Filings, newest first as in the feed, one per accession (entities merged across that accession's entries)."""
    root = _root(body)
    if root.tag != f"{ATOM}feed":
        raise FeedError(f"EDGAR latest filings: expected an Atom feed, got <{root.tag}>")
    by_acc: dict[str, FeedItem] = {}
    for e in root.findall(f"{ATOM}entry"):
        raw_title = re.sub(r"\s+", " ", html.unescape(_text(e, f"{ATOM}title") or "")).strip()
        m = _EDGAR_TITLE.match(raw_title)
        acc_m = _ACCESSION.search(_text(e, f"{ATOM}id") or "") or _ACCESSION.search(_text(e, f"{ATOM}summary") or "")
        if not m or not acc_m:
            continue
        accession = acc_m.group(1)
        entity = {"cik": int(m.group("cik")), "name": m.group("name").strip(), "role": m.group("role").strip()}
        item = by_acc.get(accession)
        if item is not None:
            if entity not in item.entities:
                item.entities.append(entity)
            continue
        cat = e.find(f"{ATOM}category")
        form = (cat.get("term") if cat is not None and cat.get("term") else m.group("form")).strip()
        summary_html = html.unescape(_text(e, f"{ATOM}summary") or "")
        items = []
        for code, text in _ITEM.findall(summary_html):
            kind, std = EIGHT_K_ITEMS.get(code, ("unknown_item", None))
            items.append({"code": code, "title": clean_text(text) or std, "type": kind})
        filed = re.search(r"Filed:\s*(?:</b>)?\s*(\d{4}-\d{2}-\d{2})", summary_html)
        size = re.search(r"Size:\s*(?:</b>)?\s*([\d.,]+\s*[KMG]?B)", summary_html)
        ts, prec = parse_time(_text(e, f"{ATOM}updated"))
        by_acc[accession] = FeedItem(
            guid=accession, title=f"{form} - {entity['name']}", link=_atom_link(e), summary=None,
            published_ns=ts, published_precision=prec, category=form, form=form, accession=accession, items=items,
            entities=[entity], filed_date=filed.group(1) if filed else None, size=size.group(1) if size else None)
    for item in by_acc.values():   # the headline names the issuer side first (Issuer, Subject, Filer, then others)
        item.entities.sort(key=lambda en: ROLE_ORDER.get(en["role"].lower(), 9))
        item.title = f"{item.form} - {item.entities[0]['name']}"
    return list(by_acc.values())


ROLE_ORDER = {"issuer": 0, "subject": 1, "filer": 2, "filed by": 3, "reporting": 4}
