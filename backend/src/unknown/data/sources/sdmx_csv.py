"""Parsing of SDMX-CSV data messages (OECD ``format=csvfile``, BIS ``format=csv``).

Both carry one observation per row with dimension columns (``REF_AREA``, ``FREQ``, ...),
``TIME_PERIOD`` and ``OBS_VALUE`` (decimal text), plus attributes such as ``OBS_STATUS`` and
``UNIT_MULT``. Values are parsed exactly; a non-zero ``UNIT_MULT`` or an unparseable value is
skipped with a reason; an empty ``OBS_VALUE`` is reported as missing, never zero."""
from __future__ import annotations

import csv
import io

from .macro_common import exact


def parse(body: bytes) -> tuple[list[dict], list[dict]]:
    """(rows, skipped). A row is {area, period, value (Fixed|None), obs_status, title, raw}."""
    text = body.decode("utf-8-sig")
    rows, skipped = [], []
    for r in csv.DictReader(io.StringIO(text)):
        area, period, raw = r.get("REF_AREA"), (r.get("TIME_PERIOD") or "").strip(), (r.get("OBS_VALUE") or "").strip()
        if not area or not period:
            skipped.append({"row": r.get("KEY") or area, "reason": "no REF_AREA or TIME_PERIOD"})
            continue
        if (r.get("UNIT_MULT") or "0").strip() not in ("0", ""):
            skipped.append({"area": area, "period": period, "reason": f"unexpected UNIT_MULT {r.get('UNIT_MULT')}"})
            continue
        value = None
        if raw and raw.lower() != "nan":
            try:
                value = exact(raw)
            except ValueError as exc:
                skipped.append({"area": area, "period": period, "reason": str(exc)})
                continue
        rows.append({"area": area, "period": period, "value": value, "obs_status": (r.get("OBS_STATUS") or "").strip(),
                     "title": (r.get("TITLE") or "").strip(), "source_ref": (r.get("SOURCE_REF") or "").strip(), "raw": r})
    return rows, skipped
