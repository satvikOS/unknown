"""Publish the verified book-review findings as docs/design/book-resolutions.md.

  python3 scripts/book_review/build_resolutions.py [--check] [--write]
  python3 scripts/book_review/build_resolutions.py --source <dir with lens JSON> [--dispositions file.json] [--write]

docs/design/book-review-findings.json is the record. Each finding keeps its stable
id BR-<LENS>-<NN> (order of the verified lens file) and carries a ``disposition``
object written by checking the current code, tests and ADRs:

  status        RESOLVED_IN_CODE | RESOLVED_BY_ADR | ACCEPTED_LIMITATION | OPEN
  resolution    what was decided or implemented (for OPEN: what already exists)
  where         repo-relative "path", "path:line", "path:a-b" or "path#text in file"
  tests         "backend/tests/<file>::<Class>::<test>" (" [IBKR venv]" marks tests
                that run only where the official ibapi client is installed)
  residual      optional: a gap left inside an otherwise resolved finding
  owner/missing required for OPEN
  limitation/documented_in   required for ACCEPTED_LIMITATION
  verified_against / verified_on

Without --source the document is rendered from that file. With --source the
findings are rebuilt from the reviewer lens files and each id keeps its
disposition from --dispositions or, failing that, from the existing record. A
finding without a disposition is shown as "pending triage", never as resolved.

--check validates every disposition mechanically and exits non-zero on any
problem: unknown status, missing required fields, a RESOLVED_IN_CODE without a
test, a RESOLVED_BY_ADR without an ADR reference, a cited file or line that does
not exist, an anchor text that is not in its file, or a cited test whose class
or method does not exist. It checks references, not behaviour; the cited tests
are run by the normal suites."""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "docs" / "design" / "book-resolutions.md"
FINDINGS = ROOT / "docs" / "design" / "book-review-findings.json"
LENS_CODES = {"money-ledger": "LED", "orders-broker-recon": "ORD", "sessions-policy-control": "SES",
              "data-research": "DAT", "agents-security-ops-law": "OPS", "reference-code": "REF"}
SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}
STATUSES = ("RESOLVED_IN_CODE", "RESOLVED_BY_ADR", "ACCEPTED_LIMITATION", "OPEN")
PENDING = "pending triage"
VENV_NOTE = " [IBKR venv]"
_LINES = re.compile(r"^(?P<path>[^#:]+?)(?::(?P<a>\d+)(?:-(?P<b>\d+))?)?(?:#(?P<anchor>.+))?$")


# ------------------------------------------------------------------ load
def load_lenses(source: Path) -> list[dict]:
    items = []
    for lens, code in LENS_CODES.items():
        f = source / f"{lens}.json"
        if not f.exists():
            continue
        data = json.loads(f.read_text())
        for i, x in enumerate(data.get("confirmed", []), start=1):
            items.append({"id": f"BR-{code}-{i:02d}", "lens": lens, **x})
    return items


def load_record(path: Path = FINDINGS) -> list[dict]:
    return json.loads(path.read_text())


def attach(items: list[dict], dispositions: dict) -> list[dict]:
    return [{**{k: v for k, v in it.items() if k != "disposition"},
             **({"disposition": dispositions[it["id"]]} if it["id"] in dispositions else {})} for it in items]


# -------------------------------------------------------------- validate
def _check_ref(ref: str, root: Path) -> str | None:
    m = _LINES.match(ref)
    if not m:
        return f"unparseable reference {ref!r}"
    path = root / m["path"]
    if not path.is_file():
        return f"missing file {m['path']}"
    text = path.read_text(encoding="utf-8")
    if m["a"]:
        n = text.count("\n") + (0 if text.endswith("\n") else 1)
        a, b = int(m["a"]), int(m["b"] or m["a"])
        if not 1 <= a <= b <= n:
            return f"{ref}: lines {a}-{b} outside 1-{n}"
    if m["anchor"] and m["anchor"].lower() not in text.lower():
        return f"{ref}: anchor text not found"
    return None


def _check_test(ref: str, root: Path) -> str | None:
    ref = ref[:-len(VENV_NOTE)] if ref.endswith(VENV_NOTE) else ref
    parts = ref.split("::")
    if len(parts) != 3 or not parts[0].startswith("backend/tests/"):
        return f"test reference must be backend/tests/<file>::<Class>::<test>: {ref!r}"
    path = root / parts[0]
    if not path.is_file():
        return f"missing test file {parts[0]}"
    src = path.read_text(encoding="utf-8")
    cls = re.search(rf"^class {re.escape(parts[1])}\b.*?(?=^class |\Z)", src, re.S | re.M)
    if cls is None:
        return f"{ref}: class not found"
    if not re.search(rf"^    def {re.escape(parts[2])}\(", cls.group(0), re.M):
        return f"{ref}: test method not found in its class"
    return None


def validate(items: list[dict], root: Path = ROOT, *, require_all: bool = True) -> list[str]:
    """Problems with the dispositions; with require_all, a finding without one is a problem too."""
    problems: list[str] = []
    ids = [it["id"] for it in items]
    if len(ids) != len(set(ids)):
        problems.append("duplicate finding ids")
    for it in items:
        fid, d = it["id"], it.get("disposition")
        if d is None:
            if require_all:
                problems.append(f"{fid}: no disposition")
            continue
        status = d.get("status")
        if status not in STATUSES:
            problems.append(f"{fid}: status {status!r} not in {STATUSES}")
            continue
        for field in ("resolution", "where", "verified_against", "verified_on"):
            if not d.get(field):
                problems.append(f"{fid}: {field} is required")
        where, tests = d.get("where", []), d.get("tests", [])
        if status == "RESOLVED_IN_CODE":
            if not tests:
                problems.append(f"{fid}: RESOLVED_IN_CODE needs at least one test that exercises it")
            if not any(_LINES.match(w) and _LINES.match(w)["a"] and w.startswith(("backend/", ".github/")) for w in where):
                problems.append(f"{fid}: RESOLVED_IN_CODE needs a code reference with a line number")
        if status == "RESOLVED_BY_ADR" and not any(w.startswith("docs/adr/") for w in where):
            problems.append(f"{fid}: RESOLVED_BY_ADR needs a docs/adr reference")
        if status == "ACCEPTED_LIMITATION" and not (d.get("limitation") and d.get("documented_in")):
            problems.append(f"{fid}: ACCEPTED_LIMITATION needs limitation and documented_in")
        if status == "OPEN" and not (d.get("owner") and d.get("missing")):
            problems.append(f"{fid}: OPEN needs owner and missing")
        for w in where:
            p = _check_ref(w, root)
            if p:
                problems.append(f"{fid}: {p}")
        for t in tests:
            p = _check_test(t, root)
            if p:
                problems.append(f"{fid}: {p}")
    return problems


# ---------------------------------------------------------------- render
def _cell(text: str) -> str:
    return text.replace("|", "/").replace("\n", " ")


def _short_test(t: str) -> str:
    venv = t.endswith(VENV_NOTE)
    t = t[:-len(VENV_NOTE)] if venv else t
    file, _, rest = t.removeprefix("backend/tests/").partition("::")
    return f"`{file.removesuffix('.py')}::{rest.split('::')[-1]}`" + (" (IBKR venv)" if venv else "")


def render(items: list[dict]) -> str:
    counts: dict[str, int] = {}
    for it in items:
        s = it.get("disposition", {}).get("status", PENDING)
        counts[s] = counts.get(s, 0) + 1
    order = [s for s in STATUSES + (PENDING,) if s in counts]
    basis = sorted({(d["verified_against"], d["verified_on"]) for d in (it.get("disposition") for it in items) if d})
    lines = [
        "# Book contradictions, omissions and resolutions",
        "",
        "Source: independent review of the engineering book (`docs/book`), contracts, configuration and reference code by six",
        "lens reviewers on 2026-10-22, each finding re-checked by an adversarial verifier (quotes and arithmetic re-done).",
        f"{len(items)} findings were confirmed; refuted findings are not listed. The book itself is preserved unchanged on",
        "`docs/technical-book-v1`; resolutions are adopted through ADRs and code on the implementation branch.",
        "",
        "Dispositions: `RESOLVED_IN_CODE` (cited code plus at least one test that exercises it), `RESOLVED_BY_ADR` (decided in",
        "the cited ADR section), `ACCEPTED_LIMITATION` (stated limitation and where it is documented) or `OPEN` (what is",
        "missing and the increment that owns it; increments are those of `docs/requirements/dependency-graph.md`). A",
        "residual is a gap recorded inside an otherwise resolved finding. Tests marked (IBKR venv) run only where the",
        "official `ibapi` client is installed and are skipped elsewhere. Passing offline tests is evidence of offline",
        "behaviour only (ADR-0001).",
        "",
    ]
    if basis:
        lines += ["Verified against " + "; ".join(f"`{b}` on {d}" for b, d in basis) + ".", ""]
    lines += [
        "Disposition summary: " + ", ".join(f"{k}: {counts[k]}" for k in order),
        "",
        "| ID | Severity | Kind | Finding | Resolution (adopted, or reviewer proposal when untriaged) | Disposition | Where |",
        "|---|---|---|---|---|---|---|",
    ]
    for it in sorted(items, key=lambda x: (SEVERITY_ORDER.get(x["severity"], 9), x["id"])):
        d = it.get("disposition", {})
        res = d.get("resolution") or ("Proposed: " + it["proposed_resolution"])[:400]
        status = d.get("status", PENDING) + (f" (owner: {d['owner']})" if d.get("status") == "OPEN" else "")
        where = "; ".join(f"`{w}`" for w in d.get("where", []))
        lines.append(f"| {it['id']} | {it['severity']} | {it['kind']} | {_cell(it['title'])} | {_cell(res)} | "
                     f"{_cell(status)} | {_cell(where)} |")

    open_items = [it for it in items if it.get("disposition", {}).get("status") == "OPEN"]
    if open_items:
        lines += ["", "## Open items by owner", "",
                  "| Owner | ID | Severity | Missing |", "|---|---|---|---|"]
        for it in sorted(open_items, key=lambda x: (x["disposition"]["owner"], SEVERITY_ORDER.get(x["severity"], 9), x["id"])):
            lines.append(f"| {_cell(it['disposition']['owner'])} | {it['id']} | {it['severity']} | "
                         f"{_cell(it['disposition']['missing'])} |")
    residuals = [it for it in items if it.get("disposition", {}).get("residual")]
    if residuals:
        lines += ["", "## Residuals recorded on resolved findings", "",
                  "| ID | Disposition | Residual |", "|---|---|---|"]
        for it in sorted(residuals, key=lambda x: x["id"]):
            lines.append(f"| {it['id']} | {it['disposition']['status']} | {_cell(it['disposition']['residual'])} |")

    lines += ["", "## Evidence per finding", ""]
    for it in sorted(items, key=lambda x: x["id"]):
        lines.append(f"### {it['id']}: {it['title']}")
        lines.append("")
        for loc in it.get("locations", [])[:4]:
            lines.append(f"- `{loc.get('file')}`{(':' + str(loc['line'])) if loc.get('line') else ''}: \"{loc.get('quote', '')[:300]}\"")
        lines.append("")
        lines.append(it["explanation"])
        if it.get("verifier_note"):
            lines.append("")
            lines.append(f"Verifier: {it['verifier_note']}")
        d = it.get("disposition")
        lines.append("")
        if d is None:
            lines.append(f"**Disposition: {PENDING}.** Reviewer proposal: {it['proposed_resolution']}")
        else:
            lines.append(f"**Disposition: {d['status']}.** {d['resolution']}")
            if d.get("limitation"):
                lines += ["", f"- Limitation: {d['limitation']}", f"- Documented in: {d['documented_in']}"]
            if d.get("missing"):
                lines += ["", f"- Missing: {d['missing']}", f"- Owner: {d['owner']}"]
            if d.get("residual"):
                lines.append(f"- Residual: {d['residual']}")
            lines.append("- Where: " + "; ".join(f"`{w}`" for w in d["where"]))
            if d.get("tests"):
                lines.append("- Tests: " + "; ".join(_short_test(t) for t in d["tests"]))
        lines.append("")
    return "\n".join(lines) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", type=Path, help="directory with the reviewer lens JSON files")
    ap.add_argument("--dispositions", type=Path, help="JSON object {finding id: disposition} (with --source)")
    ap.add_argument("--check", action="store_true", help="validate every disposition; exit 1 on any problem")
    ap.add_argument("--write", action="store_true")
    a = ap.parse_args()
    if a.source:
        items = load_lenses(a.source)
        known = {it["id"]: it["disposition"] for it in load_record() if "disposition" in it} if FINDINGS.exists() else {}
        if a.dispositions:
            known.update(json.loads(a.dispositions.read_text()))
        items = attach(items, known)
    else:
        items = load_record()
    counts: dict[str, int] = {}
    for it in items:
        s = it.get("disposition", {}).get("status", PENDING)
        counts[s] = counts.get(s, 0) + 1
    print(f"{len(items)} findings; " + ", ".join(f"{k}: {v}" for k, v in sorted(counts.items())))
    # --check requires a disposition for every finding; --write alone validates the ones that exist.
    problems = validate(items, require_all=True) if a.check else validate(items, require_all=False) if a.write else []
    for p in problems:
        print("PROBLEM", p, file=sys.stderr)
    if a.write and not problems:
        OUT.parent.mkdir(parents=True, exist_ok=True)
        OUT.write_text(render(items))
        if a.source:
            FINDINGS.write_text(json.dumps(items, indent=2, ensure_ascii=False) + "\n")
        print(f"wrote {OUT.relative_to(ROOT)}")
    if problems:
        sys.exit(1)


if __name__ == "__main__":
    main()
