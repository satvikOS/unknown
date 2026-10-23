"""Evidence verification and untrusted-text handling (ADR-0016 section 7; CAP-MODEL-12, CAP-AGT-06, CAP-AGT-10).

Claim verification (CAP-MODEL-12 verifiers 1, 2 and 4):
  * every cited span id must exist in the supplied evidence set (fabricated ids => unsupported);
  * a quoted string must occur verbatim in a cited span after whitespace normalisation
    (runs of whitespace collapse to one space, ends stripped);
  * a numeric claim is compared exactly (``Fixed``) with a typed fact of the same concept,
    period and unit when one is supplied; a mismatch makes the claim unsupported; with no
    typed fact the number is ``uncertain``;
  * a claim is ``verified`` only if it cites existing spans, its quote matches and every
    number it states agrees with a typed fact; claims with no verified evidence are
    ``unsupported`` and are never presented as fact.

Typed provenance (research_proposal v2, finding BR-DAT-12): a span built from a store row also
carries the row's reference (``ref_type``, ``ref_id``) and times (``observed_ns``,
``available_ns``). The runner turns the spans cited by verified claims into typed evidence
references from these fields only, so text inside a span cannot change them.

Untrusted input (CAP-AGT-06): documents, tool outputs and owner uploads are data. They are
rendered into delimited data blocks whose boundary carries a random nonce the document
cannot predict, so a document cannot close its own block. Instruction-pattern screening
is informational only (it is logged, it is not a boundary): the real boundary is that
tool authority comes only from the job envelope and the tool gateway, and no code path
parses document text for permissions, budgets, routing or tool names.
"""
from __future__ import annotations

import enum
import re
import secrets
from dataclasses import dataclass, field
from typing import Callable, Iterable, Mapping

from ..domain.fixed import Fixed

_WS = re.compile(r"\s+")


def normalize_ws(text: str) -> str:
    return _WS.sub(" ", text).strip()


@dataclass(frozen=True)
class EvidenceSpan:
    span_id: str
    document_id: str
    dataset_id: str
    text: str
    content_hash: str = ""
    source: str = ""
    # Typed provenance for research_proposal v2. The evidence builder sets these from the store row the span
    # was rendered from (never from span text or model output). A span without them can be quoted and
    # verified, but it never becomes a typed evidence reference.
    ref_type: str = ""                  # "observation" (a research-store observation) or "document_span"
    ref_id: str = ""                    # e.g. "obs:42": the store record or document span the text came from
    observed_ns: int | None = None      # when the observed fact happened (period end, event time), if known
    available_ns: int | None = None     # earliest legitimate decision-use time (ADR-0015 available_time)

    @property
    def typed(self) -> bool:
        return bool(self.ref_type and self.ref_id) and type(self.available_ns) is int


@dataclass(frozen=True)
class TypedFact:
    """A typed value from a parser (e.g. an XBRL fact): exact decimal, unit, period, concept."""
    concept: str
    period: str
    unit: str
    value: Fixed


@dataclass(frozen=True)
class NumericClaim:
    concept: str
    period: str
    unit: str
    value: str  # decimal text as produced by the model; parsed exactly


@dataclass(frozen=True)
class Claim:
    text: str
    span_ids: tuple[str, ...]
    quote: str | None = None
    numbers: tuple[NumericClaim, ...] = ()


class ClaimStatus(enum.Enum):
    VERIFIED = "verified"
    UNCERTAIN = "uncertain"
    UNSUPPORTED = "unsupported"


@dataclass(frozen=True)
class ClaimVerdict:
    claim: Claim
    status: ClaimStatus
    notes: tuple[str, ...]


def verify_claim(claim: Claim, spans: Mapping[str, EvidenceSpan], facts: Iterable[TypedFact] = ()) -> ClaimVerdict:
    notes: list[str] = []
    unsupported = False
    uncertain = False
    if not claim.span_ids:
        return ClaimVerdict(claim, ClaimStatus.UNSUPPORTED, ("no_citation",))
    cited = []
    for sid in claim.span_ids:
        span = spans.get(sid)
        if span is None:
            unsupported = True
            notes.append(f"unknown_span:{sid}")
        else:
            cited.append(span)
    if claim.quote is not None:
        q = normalize_ws(claim.quote)
        if not q:
            unsupported = True
            notes.append("empty_quote")
        elif not any(q in normalize_ws(s.text) for s in cited):
            unsupported = True
            notes.append("quote_not_found_in_cited_spans")
    else:
        uncertain = True
        notes.append("no_quote:entailment_not_checked")
    index: dict[tuple[str, str, str], TypedFact] = {}
    for f in facts:
        index[(f.concept, f.period, f.unit)] = f
    for n in claim.numbers:
        try:
            value = Fixed.parse(n.value)
        except (ValueError, TypeError):
            unsupported = True
            notes.append(f"unparseable_number:{n.value!r}")
            continue
        fact = index.get((n.concept, n.period, n.unit))
        if fact is None:
            uncertain = True
            notes.append(f"no_typed_fact:{n.concept}/{n.period}/{n.unit}")
        elif fact.value != value:
            unsupported = True
            notes.append(f"numeric_mismatch:{n.concept}/{n.period}: claimed {value} fact {fact.value}")
        else:
            notes.append(f"numeric_match:{n.concept}/{n.period}")
    if unsupported:
        status = ClaimStatus.UNSUPPORTED
    elif uncertain:
        status = ClaimStatus.UNCERTAIN
    else:
        status = ClaimStatus.VERIFIED
    return ClaimVerdict(claim, status, tuple(notes))


def verify_claims(claims: Iterable[Claim], spans: Iterable[EvidenceSpan], facts: Iterable[TypedFact] = ()) -> list[ClaimVerdict]:
    by_id = {s.span_id: s for s in spans}
    facts = tuple(facts)
    return [verify_claim(c, by_id, facts) for c in claims]


# ============================================================================ untrusted text
INJECTION_PATTERNS = tuple(re.compile(p, re.IGNORECASE) for p in (
    r"ignore (all |any )?(previous|prior|above) (instructions|prompts?)",
    r"disregard (the |all )?(system|previous|prior)",
    r"\bsystem prompt\b",
    r"\byou are now\b",
    r"\b(submit|place|send|execute)\b.{0,30}\b(order|trade)s?\b",
    r"\b(increase|raise|change)\b.{0,30}\b(budget|limit|cap|permission)s?\b",
    r"\bcall (the )?tool\b",
    r"\bapi[_ -]?key\b",
))


def screen(text: str) -> tuple[str, ...]:
    """Informational instruction-pattern flags (CAP-AGT-06 secondary layer; never a boundary)."""
    return tuple(p.pattern for p in INJECTION_PATTERNS if p.search(text))


@dataclass(frozen=True)
class DataBlock:
    text: str
    nonce: str
    flags: tuple[str, ...]


def render_data_block(*, kind: str, block_id: str, dataset_id: str, body: str,
                      nonce_source: Callable[[int], str] = secrets.token_hex) -> DataBlock:
    """Wrap untrusted text in a delimited block whose nonce cannot appear in the body."""
    if kind not in ("evidence_span", "tool_result", "document"):
        raise ValueError("unknown data block kind")
    for _ in range(8):
        nonce = nonce_source(16)
        if nonce not in body and nonce not in block_id and nonce not in dataset_id:
            break
    else:  # pragma: no cover - 8 collisions of a 128-bit nonce
        raise RuntimeError("could not choose a delimiter nonce")
    header = f"<<<UNTRUSTED {kind} id={block_id} dataset={dataset_id} nonce={nonce}>>>"
    footer = f"<<<END UNTRUSTED nonce={nonce}>>>"
    return DataBlock(f"{header}\n{body}\n{footer}", nonce, screen(body))


@dataclass
class InjectionLog:
    """Records informational screening hits for the evaluation suite (attempt_rate, detection_recall)."""
    hits: list[dict] = field(default_factory=list)

    def record(self, block_id: str, flags: tuple[str, ...]) -> None:
        if flags:
            self.hits.append({"block_id": block_id, "flags": list(flags)})
