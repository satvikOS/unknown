"""Publish the verified book-review findings as docs/design/book-resolutions.md.

  python3 scripts/book_review/build_resolutions.py --source <dir with lens JSON> [--dispositions dispositions.json] [--write]

Each finding gets a stable id BR-<LENS>-<NN> (order of the verified file).
Dispositions (resolved/partial/open + where) come from a separate JSON file,
keyed by id, produced by checking the current code and ADRs; a finding without
a disposition is shown as "pending triage" rather than assumed resolved."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "docs" / "design" / "book-resolutions.md"
LENS_CODES = {"money-ledger": "LED", "orders-broker-recon": "ORD", "sessions-policy-control": "SES",
              "data-research": "DAT", "agents-security-ops-law": "OPS", "reference-code": "REF"}
SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}


def load(source: Path) -> list[dict]:
    items = []
    for lens, code in LENS_CODES.items():
        f = source / f"{lens}.json"
        if not f.exists():
            continue
        data = json.loads(f.read_text())
        for i, x in enumerate(data.get("confirmed", []), start=1):
            items.append({"id": f"BR-{code}-{i:02d}", "lens": lens, **x})
    return items


def render(items: list[dict], dispositions: dict) -> str:
    counts = {}
    for it in items:
        d = dispositions.get(it["id"], {}).get("status", "pending triage")
        counts[d] = counts.get(d, 0) + 1
    lines = [
        "# Book contradictions, omissions and resolutions",
        "",
        "Source: independent review of the engineering book (`docs/book`), contracts, configuration and reference code by six",
        "lens reviewers on 2026-10-22, each finding re-checked by an adversarial verifier (quotes and arithmetic re-done).",
        f"{len(items)} findings were confirmed; refuted findings are not listed. The book itself is preserved unchanged on",
        "`docs/technical-book-v1`; resolutions are adopted through ADRs and code on the implementation branch.",
        "",
        "Disposition summary: " + ", ".join(f"{k}: {v}" for k, v in sorted(counts.items())),
        "",
        "| ID | Severity | Kind | Finding | Resolution (adopted, or reviewer proposal when untriaged) | Disposition | Where |",
        "|---|---|---|---|---|---|---|",
    ]
    for it in sorted(items, key=lambda x: (SEVERITY_ORDER.get(x["severity"], 9), x["id"])):
        d = dispositions.get(it["id"], {})
        res = (d.get("resolution") or ("Proposed: " + it["proposed_resolution"])).replace("|", "/").replace("\n", " ")
        lines.append(f"| {it['id']} | {it['severity']} | {it['kind']} | {it['title'].replace('|', '/')} | {res[:400]} | "
                     f"{d.get('status', 'pending triage')} | {d.get('where', '').replace('|', '/')} |")
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
        lines.append("")
    return "\n".join(lines) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", required=True, type=Path)
    ap.add_argument("--dispositions", type=Path)
    ap.add_argument("--write", action="store_true")
    a = ap.parse_args()
    items = load(a.source)
    dispositions = json.loads(a.dispositions.read_text()) if a.dispositions and a.dispositions.exists() else {}
    text = render(items, dispositions)
    print(f"{len(items)} findings; {len(dispositions)} dispositions")
    if a.write:
        OUT.parent.mkdir(parents=True, exist_ok=True)
        OUT.write_text(text)
        (OUT.parent / "book-review-findings.json").write_text(json.dumps(items, indent=2, ensure_ascii=False) + "\n")
        print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
