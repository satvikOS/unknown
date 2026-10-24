"""Deterministic post-check of an analyst answer (rule: every claim cites the tool results it used).

The answer is checked line by line (a bullet or a paragraph is one line):
  * ``cited``      the line names at least one ref ([R1], [R2, R5]) of a tool result this job produced;
  * ``unsourced``  a statement with no valid ref: flagged in the terminal, never shown as sourced;
  * ``structural`` headings (``#`` or a line that is only bold text), list introductions ending with ":" and very
                   short lines (not statements).
Refs that name no tool result of the job are listed as ``invalid_refs``. Every number in a cited line is looked up in
the cited results' content: an exact match, or the source value rounded at the precision the line states (with
thousand/million/billion scaling and percentages also read as fractions). Numbers that match nothing are listed per
line, so a value the model changed or invented is visible. Integers below 10 without a unit are not checked (they are
counts and ordinals far more often than data).
"""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Iterable, Mapping

REF_GROUP = re.compile(r"\[(\s*R\d{1,4}(?:\s*[,;]\s*R\d{1,4})*\s*)\]")
_REF = re.compile(r"R\d{1,4}")
_CLAIM_NUMBER = re.compile(r"(?<![\w.])(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d+))?(?!\d)\s*(%|percent\b|billion\b|million\b|thousand\b|bn\b|mn\b|[bmk]\b)?",
                           re.IGNORECASE)
_SOURCE_NUMBER = re.compile(r"\d+(?:\.\d+)?")      # magnitudes: a sign or a date hyphen is not part of it
_SCALE = {"billion": Decimal(10) ** 9, "bn": Decimal(10) ** 9, "b": Decimal(10) ** 9, "million": Decimal(10) ** 6,
          "mn": Decimal(10) ** 6, "m": Decimal(10) ** 6, "thousand": Decimal(1000), "k": Decimal(1000)}
MIN_STATEMENT_CHARS = 25
_BOLD_HEADING = re.compile(r"\*\*[^*]+\*\*:?")          # a line that is only bold text is a sub-heading


def refs_in(text: str) -> list[str]:
    out: list[str] = []
    for group in REF_GROUP.findall(text):
        for ref in _REF.findall(group):
            if ref not in out:
                out.append(ref)
    return out


def source_numbers(texts: Iterable[str]) -> set[Decimal]:
    out: set[Decimal] = set()
    for t in texts:
        for m in _SOURCE_NUMBER.findall(re.sub(r"(?<=\d),(?=\d{3})", "", t)):
            try:
                out.add(Decimal(m))
            except InvalidOperation:  # pragma: no cover - the pattern only matches decimals
                continue
    return out


def _matches(value: Decimal, decimals: int, unit: str | None, sources: set[Decimal]) -> bool:
    candidates = [(value, Decimal(1))]
    u = (unit or "").lower()
    if u in _SCALE:
        candidates = [(value * _SCALE[u], _SCALE[u])]
    elif u in ("%", "percent"):
        candidates = [(value, Decimal(1)), (value / 100, Decimal("0.01"))]
    for target, scale in candidates:
        half = (Decimal(1).scaleb(-decimals) * scale) / 2
        if target in sources:
            return True
        if any(abs(s - target) <= half for s in sources):
            return True
    return False


def check_numbers(line: str, sources: set[Decimal]) -> tuple[int, list[str]]:
    checked, unmatched = 0, []
    text = REF_GROUP.sub(" ", line)
    for m in _CLAIM_NUMBER.finditer(text):
        whole, frac, unit = m.group(1), m.group(2), m.group(3)
        value = Decimal(whole.replace(",", "") + (f".{frac}" if frac else ""))
        if not frac and not unit and value < 10:
            continue
        checked += 1
        if not _matches(value, len(frac or ""), unit, sources):
            unmatched.append(m.group(0).strip())
    return checked, unmatched


def _structural(line: str) -> bool:
    s = line.strip()
    return (not s or s.startswith("#") or s.endswith(":") or len(REF_GROUP.sub("", s).strip(" -*•")) < MIN_STATEMENT_CHARS
            or set(s) <= set("-*_=| ") or _BOLD_HEADING.fullmatch(s) is not None)


def check_answer(text: str, results: Mapping[str, str]) -> dict:
    """``results`` maps each ref of the job to the content the model saw. Returns the per-line verdicts."""
    lines, used, invalid = [], [], []
    numbers_cache: dict[tuple[str, ...], set[Decimal]] = {}
    for i, raw in enumerate(text.splitlines()):
        refs = refs_in(raw)
        valid = [r for r in refs if r in results]
        invalid.extend(r for r in refs if r not in results and r not in invalid)
        for r in valid:
            if r not in used:
                used.append(r)
        if not valid and _structural(raw):
            continue
        entry = {"line": i, "refs": valid, "status": "cited" if valid else "unsourced"}
        if valid:
            key = tuple(sorted(valid))
            if key not in numbers_cache:
                numbers_cache[key] = source_numbers(results[r] for r in key)
            checked, unmatched = check_numbers(raw, numbers_cache[key])
            entry["numbers_checked"] = checked
            entry["numbers_unmatched"] = unmatched
        lines.append(entry)
    return {"lines": lines, "refs_used": used, "invalid_refs": invalid,
            "statements": len(lines), "cited": sum(1 for x in lines if x["status"] == "cited"),
            "unsourced": sum(1 for x in lines if x["status"] == "unsourced"),
            "numbers_unmatched": sum(len(x.get("numbers_unmatched", [])) for x in lines)}
