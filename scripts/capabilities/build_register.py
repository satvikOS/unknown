"""Import verified capability research into the persistent register (PAR-01..PAR-03).

  python3 scripts/capabilities/build_register.py --source <dir of verified area JSON> [--write]

Idempotent: a record keeps its stable ID (CAP-<AREA>-<NN>) and its implementation
status/evidence/gaps across re-imports, matched by area+slug. New records get the
next free number. The importer never upgrades a status; statuses change only by
editing register.json with evidence (ADR-0001). It also renders
docs/capabilities/README.md, a readable index with separate parity dimensions.
"""
from __future__ import annotations

import argparse
import json
import re
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


def render_readme(reg: dict) -> str:
    caps = reg["capabilities"]
    counts = defaultdict(lambda: defaultdict(int))
    for c in caps:
        counts[c["area"]][c["implementation"]["status"]] += 1
    lines = [
        "# Capability register",
        "",
        "Machine-readable source: `register.json` (import tool: `scripts/capabilities/build_register.py`).",
        "Reference products: Bloomberg Terminal, LSEG Workspace, S&P Capital IQ Pro and IBKR, scoped only by what their",
        "public documentation and product pages show (URLs and quotes per capability). Status vocabulary follows ADR-0001;",
        "nothing is `paper-verified` or `live-verified` yet.",
        "",
        "Parity is tracked in six separate dimensions per capability (functional, data coverage, data quality, latency,",
        "usability, operational reliability). Proprietary datasets, interfaces and closed-network functions are not copied;",
        "each capability records lawful alternatives and what cannot be replicated.",
        "",
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
    lines += ["", "## Capabilities", ""]
    current = None
    for c in caps:
        if c["area"] != current:
            current = c["area"]
            lines += ["", f"### {current}", "", "| ID | Priority | Capability | Reference scope | Status | Main gap |", "|---|---|---|---|---|---|"]
        refs = sorted({r["product"] for r in c.get("reference_scope", [])})
        gap = (c["implementation"].get("gaps") or [""])[0]
        title = c["title"].replace("|", "/")
        lines.append(f"| {c['id']} | {c.get('priority', '')} | {title} | {', '.join(refs)[:80]} | {c['implementation']['status']} | {gap[:80]} |")
    return "\n".join(lines) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", required=True, type=Path)
    ap.add_argument("--write", action="store_true")
    a = ap.parse_args()
    reg = import_records(a.source, load_existing())
    print(f"{len(reg['capabilities'])} capabilities across {len(reg['areas'])} areas")
    if a.write:
        REGISTER.parent.mkdir(parents=True, exist_ok=True)
        REGISTER.write_text(json.dumps(reg, indent=2, ensure_ascii=False) + "\n")
        README.write_text(render_readme(reg))
        print(f"wrote {REGISTER} and {README}")


if __name__ == "__main__":
    main()
