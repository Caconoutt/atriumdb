"""Generate the committed measure-resolution seed file from the planning workbook.

This is a one-off build step, run by a maintainer whenever the corrected workbook
(`AtriumDB_VitalSign_Mapping_corrected.xlsx`) changes. Its output,
`measure_mapping/seed_measures.csv`, is the canonical init input consumed at runtime
by `initialize_measure_mapping` (see the spec, sections 4 and 5).

Design rules enforced here (so the runtime code can stay simple):

* The seed is keyed on the **triple** `(tag, freq_nhz, unit_code)` and never on any
  AtriumDB `measure_id`. The integer ids in the workbook are reference data for one
  instance only and are intentionally dropped.
* `freq_nhz` is written directly (not `freq_hz`), with the display-rounding of the
  workbook corrected: the numeric parameters are sampled at 1000/1024 Hz = 0.9765625 Hz
  which is exactly 976_562_500 nHz, but the workbook rounds it to 0.976562. Waveforms
  are 125 Hz = 125_000_000_000 nHz.
* `unit_code` is the AtriumDB MDC unit string (e.g. ``MDC_DIM_MMHG``) -- the value the
  SDK ``units`` argument expects -- NOT the human label and NOT the numeric ISO code.
* Every tab is represented, one row per measure. `measure_group` only organises the UI;
  it never gates resolution.

Usage:
    python dashboard/scripts/generate_seed.py [path/to/workbook.xlsx] [path/to/out.csv]
"""
from __future__ import annotations

import csv
import sys
from pathlib import Path

import openpyxl

# --- normalisation maps ----------------------------------------------------

# Human label -> AtriumDB MDC unit string. Used for tabs that only carry the label.
LABEL_TO_UNIT_CODE = {
    "mmHg": "MDC_DIM_MMHG",
    "bpm": "MDC_DIM_BEAT_PER_MIN",
    "kPa": "MDC_DIM_KILO_PASCAL",
    "%": "MDC_DIM_PERCENT",
    "°C": "MDC_DIM_DEGC",
}

# Workbook vital-sign label -> stable dashboard key (only the four core vitals).
VITAL_SIGN_KEY = {
    "Heart Rate": "heart_rate",
    "Systolic BP": "systolic_bp",
    "Diastolic BP": "diastolic_bp",
    "Mean BP": "mean_bp",
}

KPA_TO_MMHG = 7.50062  # canonical conversion factor for kPa NIBP variants


def freq_hz_to_nhz(freq_hz: float) -> int:
    """Convert a workbook freq (Hz) to exact nanohertz, correcting display rounding."""
    if abs(freq_hz - 0.976562) < 1e-4:
        # 1000/1024 Hz, displayed rounded in the workbook; exact value is 0.9765625 Hz.
        return 976_562_500
    if abs(freq_hz - 125.0) < 1e-9:
        return 125_000_000_000
    # Fallback: best-effort exact conversion for any future rows.
    return int(round(freq_hz * 1_000_000_000))


def unit_code_for(label: str | None, explicit_code: str | None) -> str:
    if explicit_code:
        return explicit_code.strip()
    if label and label in LABEL_TO_UNIT_CODE:
        return LABEL_TO_UNIT_CODE[label]
    raise ValueError(f"Cannot map unit label {label!r} to an MDC unit string")


def _rows_after_header(ws, header_first_cell: str):
    """Yield dict rows from a sheet whose header row begins with ``header_first_cell``."""
    header = None
    for row in ws.iter_rows(values_only=True):
        if header is None:
            if row and row[0] == header_first_cell:
                header = list(row)
            continue
        if all(c is None for c in row):
            continue
        yield dict(zip(header, row))


def build_rows(wb) -> list[dict]:
    out: list[dict] = []

    # --- Vital Sign Mapping -> core_vital_sign --------------------------------
    for r in _rows_after_header(wb["Vital Sign Mapping"], "measure_id"):
        unit_code = unit_code_for(r.get("unit"), r.get("unit_code"))
        is_kpa = unit_code == "MDC_DIM_KILO_PASCAL"
        vital = VITAL_SIGN_KEY.get(r["vital_sign"])
        # canonical unit applies to all blood-pressure vitals (and their kPa variants);
        # heart-rate has no canonical conversion target.
        canonical = "mmHg" if vital in ("systolic_bp", "diastolic_bp", "mean_bp") else ""
        out.append({
            "tag": r["tag"],
            "freq_nhz": freq_hz_to_nhz(r["freq_hz"]),
            "unit_code": unit_code,
            "unit_label": r["unit"],
            "name": r["name"],
            "measure_group": "core_vital_sign",
            "vital_sign": vital or "",
            "review": "false",
            "conversion_factor": KPA_TO_MMHG if is_kpa else 1.0,
            "canonical_unit": canonical,
            "notes": r.get("notes") or "",
        })

    # --- Excluded - Non-systemic -> other_pressure ----------------------------
    for r in _rows_after_header(wb["Excluded - Non-systemic"], "measure_id"):
        name = f"{r['category']} ({r['component']})"
        out.append({
            "tag": r["tag"],
            "freq_nhz": freq_hz_to_nhz(r["freq_hz"]),
            "unit_code": unit_code_for(r.get("unit"), None),
            "unit_label": r["unit"],
            "name": name,
            "measure_group": "other_pressure",
            "vital_sign": "",
            "review": "false",
            "conversion_factor": 1.0,
            "canonical_unit": "",
            "notes": r.get("reason_excluded") or "",
        })

    # --- Clinical Review -> needs_review (review=true) ------------------------
    for r in _rows_after_header(wb["Clinical Review"], "measure_id"):
        out.append({
            "tag": r["tag"],
            "freq_nhz": freq_hz_to_nhz(r["freq_hz"]),
            "unit_code": unit_code_for(r.get("unit"), r.get("unit_code")),
            "unit_label": r["unit"],
            "name": r["tag"],
            "measure_group": "needs_review",
            "vital_sign": "",
            "review": "true",
            "conversion_factor": 1.0,
            "canonical_unit": "",
            "notes": "; ".join(x for x in (r.get("review_reason"), r.get("notes")) if x),
        })

    # --- Waveforms (Phase 2) -> waveform --------------------------------------
    for r in _rows_after_header(wb["Waveforms (Phase 2)"], "measure_id"):
        out.append({
            "tag": r["tag"],
            "freq_nhz": freq_hz_to_nhz(r["freq_hz"]),
            "unit_code": unit_code_for(r.get("unit"), None),
            "unit_label": r["unit"],
            "name": r["tag"],
            "measure_group": "waveform",
            "vital_sign": "",
            "review": "false",
            "conversion_factor": 1.0,
            "canonical_unit": "",
            "notes": r.get("notes") or "",
        })

    return out


FIELDNAMES = [
    "tag", "freq_nhz", "unit_code", "unit_label", "name",
    "measure_group", "vital_sign", "review", "conversion_factor",
    "canonical_unit", "notes",
]


def main() -> None:
    here = Path(__file__).resolve()
    repo = here.parents[2]
    workbook = Path(sys.argv[1]) if len(sys.argv) > 1 else repo / "AtriumDB_VitalSign_Mapping_corrected.xlsx"
    out_csv = Path(sys.argv[2]) if len(sys.argv) > 2 else here.parents[1] / "measure_mapping" / "seed_measures.csv"

    wb = openpyxl.load_workbook(workbook, data_only=True)
    rows = build_rows(wb)

    # Enforce the invariant: the triple is unique across the whole seed.
    seen: dict[tuple, dict] = {}
    for r in rows:
        key = (r["tag"], r["freq_nhz"], r["unit_code"])
        if key in seen:
            raise ValueError(f"Duplicate triple in seed: {key}")
        seen[key] = r

    out_csv.parent.mkdir(parents=True, exist_ok=True)
    with out_csv.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=FIELDNAMES)
        writer.writeheader()
        for r in rows:
            writer.writerow(r)

    print(f"Wrote {len(rows)} measures to {out_csv}")
    by_group: dict[str, int] = {}
    for r in rows:
        by_group[r["measure_group"]] = by_group.get(r["measure_group"], 0) + 1
    for g, n in sorted(by_group.items()):
        print(f"  {g}: {n}")


if __name__ == "__main__":
    main()
