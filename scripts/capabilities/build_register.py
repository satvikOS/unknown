"""Import verified capability research into the persistent register (PAR-01..PAR-03).

  python3 scripts/capabilities/build_register.py --source <dir of verified area JSON> [--write]
  python3 scripts/capabilities/build_register.py [--check] [--run-tests] [--write]

Idempotent: a record keeps its stable ID (CAP-<AREA>-<NN>) and its implementation
status/evidence/gaps across re-imports, matched by area+slug. New records get the
next free number. The importer never upgrades a status; statuses change only by
editing register.json with evidence (ADR-0001). It also renders
docs/capabilities/README.md, a readable index with separate parity dimensions.
Without --source, --write re-renders the README from register.json.

Implementation evidence (``implementation`` of each record):

  status            ADR-0001 word; see STATUS_RULES
  files             repo-relative paths of the implementing code (files or directories)
  tests             "backend/tests/<file>.py::<Class>.<test>" (" [IBKR venv]" marks tests that
                    run only where the official ibapi client is installed), or
                    "terminal/src/<file>.test.ts::<describe> > <it>" / "terminal/e2e/<file>.spec.ts::<title>"
  evidence          what the files and tests show, including recorded live runs
  gaps              what the capability still lacks (required above proposed)
  verified_against  "<branch>@<commit>" the status was checked against; updated: date

--check validates every record mechanically and exits non-zero on any problem: an
unknown status, paper-verified or live-verified without external evidence, a status
above proposed without files, gaps or verified_against, a tested status without
tests, a cited path that does not exist, or a cited test whose class/method (Python)
or title (TypeScript) is not in its file. It checks references, not behaviour.
--run-tests also runs every cited backend test with this interpreter and fails if one
does not pass (a skip is accepted only for tests marked [IBKR venv]); terminal tests
run with `npm test` and `npm run test:e2e` in terminal/.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
REGISTER = ROOT / "docs" / "capabilities" / "register.json"
README = ROOT / "docs" / "capabilities" / "README.md"

AREA_CODES = {
    "instruments": "INS", "market-data": "MKT", "company-research": "CO", "news-documents": "NEWS",
    "macro-rates-fx": "MAC", "portfolio-risk": "PORT", "multiasset": "MA", "strategy-research": "RES",
    "execution-oms": "OMS", "workspace-ux": "UX", "agent-operations": "AGT", "ibkr-integration": "IBKR",
    "data-providers": "DATA", "model-providers": "MODEL", "regulatory": "LAW", "ops-aws": "OPS",
}
STATUSES = ["proposed", "specified", "implemented", "tested", "paper-verified", "live-verified"]
STATUS_RULES = [
    "`proposed`: researched and scoped; nothing built.",
    "`specified`: an accepted ADR or specification fixes the behaviour; no code.",
    "`implemented`: code for part of the capability exists, but either its main behaviour is not exercised by tests or the part "
    "that exists is a small fraction of the capability. The cited tests cover only what exists.",
    "`tested`: the capability's main behaviour exists and passing offline deterministic tests exercise it. `tested` does not mean "
    "complete: the gaps list the acceptance criteria and parts still missing.",
    "`paper-verified` / `live-verified`: verified against an IBKR paper or live account; needs recorded external evidence. "
    "Offline tests can never produce these.",
]
VENV_NOTE = " [IBKR venv]"
EXTERNAL_STATUSES = ("paper-verified", "live-verified")


def load_existing() -> dict:
    if REGISTER.exists():
        return json.loads(REGISTER.read_text())
    return {"schema_version": 1, "description": "", "capabilities": []}


def import_records(source: Path, existing: dict) -> dict:
    by_key = {(c["area"], c["slug"]): c for c in existing["capabilities"]}
    next_n: dict[str, int] = defaultdict(int)
    for c in existing["capabilities"]:
        m = re.match(r"CAP-([A-Z]+)-(\d+)$", c["id"])
        if m:
            next_n[m.group(1)] = max(next_n[m.group(1)], int(m.group(2)))
    sources_meta = {}
    for f in sorted(source.glob("*.json")):
        area = f.stem
        if area not in AREA_CODES:
            continue
        data = json.loads(f.read_text())
        sources_meta[area] = {"verification": data.get("verification"), "unfetchable": data.get("unfetchable", []),
                              "notes": data.get("notes", "")}
        for r in data.get("records", []):
            key = (area, r["slug"])
            prior = by_key.get(key)
            code = AREA_CODES[area]
            if prior is None:
                next_n[code] += 1
                cid = f"CAP-{code}-{next_n[code]:02d}"
                impl = {"status": "proposed", "evidence": [], "gaps": ["not yet implemented"], "requirements": []}
            else:
                cid = prior["id"]
                impl = prior["implementation"]
            ver = data.get("verification", {}) or {}
            record = {"id": cid, "area": area, "slug": r["slug"], **{k: v for k, v in r.items() if k != "slug"},
                      "source_verification": {"verified_on": ver.get("verified_on"), "method": "independent re-fetch by a second agent"},
                      "implementation": impl}
            by_key[key] = record
    caps = sorted(by_key.values(), key=lambda c: (list(AREA_CODES).index(c["area"]) if c["area"] in AREA_CODES else 99, c["id"]))
    return {
        "schema_version": 1,
        "description": ("Project Unknown capability register. Each capability names its reference product scope with "
                        "fetched primary sources, its lawful implementation path, and its implementation status. "
                        "Parity dimensions are tracked separately; a similar-looking screen is not parity evidence."),
        "status_vocabulary": STATUSES,
        "areas": sources_meta,
        "capabilities": caps,
    }


# -------------------------------------------------------------- validate
def _python_test(ref: str, root: Path) -> str | None:
    m = re.match(r"^(backend/tests/[\w/]+\.py)::(\w+)\.(\w+)$", ref)
    if not m:
        return f"test reference must be backend/tests/<file>.py::<Class>.<test>: {ref!r}"
    path = root / m[1]
    if not path.is_file():
        return f"missing test file {m[1]}"
    cls = re.search(rf"^class {re.escape(m[2])}\b.*?(?=^class |\Z)", path.read_text(encoding="utf-8"), re.S | re.M)
    if cls is None:
        return f"{ref}: class not found"
    if not re.search(rf"^    def {re.escape(m[3])}\(", cls.group(0), re.M):
        return f"{ref}: test method not found in its class"
    return None


def _terminal_test(ref: str, root: Path) -> str | None:
    path, _, title = ref.partition("::")
    if not re.match(r"^terminal/(src/.+\.test\.tsx?|e2e/.+\.spec\.ts)$", path) or not title:
        return f"terminal test reference must be terminal/src/<file>.test.ts::<describe> > <it> or terminal/e2e/<file>.spec.ts::<title>: {ref!r}"
    if not (root / path).is_file():
        return f"missing test file {path}"
    text = (root / path).read_text(encoding="utf-8")
    for part in title.split(" > "):
        if part not in text:
            return f"{ref}: title {part!r} not found in the file"
    return None


def _check_test(ref: str, root: Path) -> str | None:
    if ref.startswith("terminal/"):
        return _terminal_test(ref, root)
    return _python_test(ref[:-len(VENV_NOTE)] if ref.endswith(VENV_NOTE) else ref, root)


def validate(reg: dict, root: Path = ROOT) -> list[str]:
    """Problems with the implementation records; see the module docstring."""
    problems: list[str] = []
    ids = [c["id"] for c in reg["capabilities"]]
    if len(ids) != len(set(ids)):
        problems.append("duplicate capability ids")
    for c in reg["capabilities"]:
        cid, impl = c["id"], c.get("implementation") or {}
        status = impl.get("status")
        if status not in STATUSES:
            problems.append(f"{cid}: status {status!r} not in {STATUSES}")
            continue
        if status == "proposed":
            continue
        if status in EXTERNAL_STATUSES and not impl.get("external_evidence"):
            problems.append(f"{cid}: {status} needs recorded external_evidence (ADR-0001)")
        for field in ("files", "gaps", "verified_against", "updated"):
            if not impl.get(field):
                problems.append(f"{cid}: {field} is required above proposed")
        if status in ("tested", *EXTERNAL_STATUSES) and not impl.get("tests"):
            problems.append(f"{cid}: {status} needs at least one cited test")
        for f in impl.get("files", []):
            if not (root / f).exists():
                problems.append(f"{cid}: missing file {f}")
        for ref in impl.get("tests", []):
            p = _check_test(ref, root)
            if p:
                problems.append(f"{cid}: {p}")
    return problems


def run_cited_tests(reg: dict, root: Path = ROOT) -> list[str]:
    """Run every cited backend test with this interpreter; a skip is accepted only for [IBKR venv] tests."""
    cited: dict[str, bool] = {}
    for c in reg["capabilities"]:
        for ref in (c.get("implementation") or {}).get("tests", []):
            if ref.startswith("backend/tests/"):
                venv = ref.endswith(VENV_NOTE)
                m = re.match(r"^backend/tests/(\w+)\.py::(\w+)\.(\w+)", ref)
                if m:
                    cited[f"{m[1]}.{m[2]}.{m[3]}"] = cited.get(f"{m[1]}.{m[2]}.{m[3]}", True) and venv
    env = {**os.environ, "PYTHONPATH": "src"}
    run = subprocess.run([sys.executable, "-m", "unittest", "-v", *sorted(f"tests.{n}" for n in cited)], cwd=root / "backend",
                         env=env, capture_output=True, text=True)
    status: dict[str, str] = {}
    current = None
    for line in run.stderr.splitlines():
        m = re.match(r"^test\w* \(([\w.]+)\)", line)
        if m:
            current = m[1]
        # The verdict ends the test's line, or stands alone after output a background thread wrote meanwhile.
        s = re.search(r"(?:\.\.\. |^)(ok|skipped.*|FAIL|ERROR|expected failure|unexpected success)$", line)
        if s and current:
            status[current.removeprefix("tests.")] = s[1]
            current = None
    problems = []
    for name, venv_only in sorted(cited.items()):
        st = status.get(name)
        if st == "ok" or (st and st.startswith("skipped") and venv_only):
            continue
        problems.append(f"cited test {name}: {st or 'did not run'}")
    print(f"ran {len(cited)} cited backend tests with {sys.executable}: "
          f"{sum(1 for v in status.values() if v == 'ok')} ok, {sum(1 for v in status.values() if v.startswith('skipped'))} skipped")
    return problems


# ---------------------------------------------------------------- render
def _short_test(ref: str) -> str:
    venv = ref.endswith(VENV_NOTE)
    ref = ref[:-len(VENV_NOTE)] if venv else ref
    if ref.startswith("backend/tests/"):
        file, _, rest = ref.removeprefix("backend/tests/").partition("::")
        return f"`{file.removesuffix('.py')}::{rest}`" + (" (IBKR venv)" if venv else "")
    return f"`{ref.removeprefix('terminal/')}`"


def _cell(text: str) -> str:
    return text.replace("|", "/").replace("\n", " ")


def render_readme(reg: dict) -> str:
    caps = reg["capabilities"]
    counts = defaultdict(lambda: defaultdict(int))
    for c in caps:
        counts[c["area"]][c["implementation"]["status"]] += 1
    built = [c for c in caps if c["implementation"]["status"] != "proposed"]
    basis = sorted({(c["implementation"].get("verified_against"), c["implementation"].get("updated")) for c in built
                    if c["implementation"].get("verified_against")})
    lines = [
        "# Capability register",
        "",
        "Machine-readable source: `register.json` (import tool and checker: `scripts/capabilities/build_register.py`).",
        "Reference products: Bloomberg Terminal, LSEG Workspace, S&P Capital IQ Pro and IBKR, scoped only by what their",
        "public documentation and product pages show (URLs and quotes per capability). Status vocabulary follows ADR-0001;",
        "nothing is `paper-verified` or `live-verified` yet.",
        "",
        "Parity is tracked in six separate dimensions per capability (functional, data coverage, data quality, latency,",
        "usability, operational reliability). Proprietary datasets, interfaces and closed-network functions are not copied;",
        "each capability records lawful alternatives and what cannot be replicated.",
        "",
        "## How statuses are assigned",
        "",
        *[f"- {r}" for r in STATUS_RULES],
        "",
        "Every status above `proposed` cites the implementing files, the tests that prove it and the gaps that remain",
        "(`implementation.files`, `.tests`, `.gaps` in `register.json`). `build_register.py --check` verifies that every cited",
        "file, test class and test method exists; `--run-tests` runs the cited backend tests. Passing offline tests is evidence",
        "of offline behaviour only.",
        "",
    ]
    if basis:
        lines += ["Checked against " + "; ".join(f"`{b}` on {d}" for b, d in basis) + ".", ""]
    lines += [
        "## Summary by area",
        "",
        "| Area | Capabilities | " + " | ".join(STATUSES) + " |",
        "|---|---|" + "---|" * len(STATUSES),
    ]
    for area in AREA_CODES:
        if area not in counts:
            continue
        total = sum(counts[area].values())
        lines.append(f"| {area} | {total} | " + " | ".join(str(counts[area].get(s, 0)) for s in STATUSES) + " |")
    totals = defaultdict(int)
    for c in caps:
        totals[c["implementation"]["status"]] += 1
    lines.append(f"| **all** | {len(caps)} | " + " | ".join(str(totals.get(s, 0)) for s in STATUSES) + " |")
    lines += ["", "## Capabilities", ""]
    current = None
    for c in caps:
        if c["area"] != current:
            current = c["area"]
            lines += ["", f"### {current}", "", "| ID | Priority | Capability | Reference scope | Status | Tests | Main gap |",
                      "|---|---|---|---|---|---|---|"]
        refs = sorted({r["product"] for r in c.get("reference_scope", [])})
        impl = c["implementation"]
        gap = (impl.get("gaps") or [""])[0]
        title = _cell(c["title"])
        n_tests = len(impl.get("tests", []))
        lines.append(f"| {c['id']} | {c.get('priority', '')} | {title} | {', '.join(refs)[:80]} | {impl['status']} | "
                     f"{n_tests or ''} | {_cell(gap)[:80]} |")
    if built:
        lines += ["", "## Evidence for capabilities above `proposed`", ""]
        for c in built:
            impl = c["implementation"]
            lines += [f"### {c['id']}: {c['title']} ({impl['status']})", ""]
            lines.append("- Files: " + ", ".join(f"`{f}`" for f in impl.get("files", [])))
            if impl.get("tests"):
                lines.append("- Tests: " + "; ".join(_short_test(t) for t in impl["tests"]))
            for e in impl.get("evidence", []):
                lines.append(f"- Evidence: {e}")
            for g in impl.get("gaps", []):
                lines.append(f"- Gap: {g}")
            if impl.get("requirements"):
                lines.append("- Requirements: " + ", ".join(impl["requirements"]))
            lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", type=Path, help="directory of verified area JSON to import")
    ap.add_argument("--check", action="store_true", help="validate every implementation record; exit 1 on any problem")
    ap.add_argument("--run-tests", action="store_true", help="also run every cited backend test with this interpreter")
    ap.add_argument("--write", action="store_true")
    a = ap.parse_args()
    reg = import_records(a.source, load_existing()) if a.source else load_existing()
    counts = defaultdict(int)
    for c in reg["capabilities"]:
        counts[c["implementation"]["status"]] += 1
    print(f"{len(reg['capabilities'])} capabilities across {len(reg['areas'])} areas; "
          + ", ".join(f"{s}: {counts[s]}" for s in STATUSES if counts[s]))
    problems = validate(reg) if (a.check or a.write) else []
    if a.run_tests:
        problems += run_cited_tests(reg)
    for p in problems:
        print("PROBLEM", p, file=sys.stderr)
    if a.write and not problems:
        REGISTER.parent.mkdir(parents=True, exist_ok=True)
        if a.source:
            REGISTER.write_text(json.dumps(reg, indent=2, ensure_ascii=False) + "\n")
        README.write_text(render_readme(reg))
        print(f"wrote {README.relative_to(ROOT)}" + (f" and {REGISTER.relative_to(ROOT)}" if a.source else ""))
    if problems:
        sys.exit(1)


if __name__ == "__main__":
    main()
