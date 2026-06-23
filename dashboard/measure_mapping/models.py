"""Plain data structures used by the measure-resolution layer.

These are intentionally dependency-free so they can cross the boundary between the
init step (which touches the AtriumDB SDK) and the request-time resolver (which only
touches the dashboard DB).
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class SeedRow:
    """One row of the canonical seed file (keyed on the triple, never on an id)."""
    tag: str
    freq_nhz: int
    unit_code: str          # AtriumDB MDC unit string, e.g. "MDC_DIM_MMHG"
    unit_label: str         # human label for the UI, e.g. "mmHg"
    name: str               # display name
    measure_group: str      # core_vital_sign | other_pressure | waveform | needs_review
    vital_sign: str | None  # heart_rate | systolic_bp | diastolic_bp | mean_bp | None
    review: bool
    conversion_factor: float
    canonical_unit: str | None
    notes: str = ""

    @classmethod
    def from_csv(cls, row: dict) -> "SeedRow":
        def _opt(value: str | None) -> str | None:
            value = (value or "").strip()
            return value or None

        return cls(
            tag=row["tag"].strip(),
            freq_nhz=int(row["freq_nhz"]),
            unit_code=row["unit_code"].strip(),
            unit_label=row["unit_label"].strip(),
            name=row["name"].strip(),
            measure_group=row["measure_group"].strip(),
            vital_sign=_opt(row.get("vital_sign")),
            review=str(row.get("review", "")).strip().lower() in ("1", "true", "yes"),
            conversion_factor=float(row.get("conversion_factor") or 1.0),
            canonical_unit=_opt(row.get("canonical_unit")),
            notes=(row.get("notes") or "").strip(),
        )


@dataclass
class ResolvedMeasure:
    """The output of request-time resolution: an AtriumDB id plus retriever metadata.

    ``conversion_factor`` is returned for the downstream retrieval layer to apply; it is
    NOT applied here.
    """
    atriumdb_measure_id: int
    conversion_factor: float
    canonical_unit: str | None
    unit_code: str
    display_name: str
    measure_id: int          # stable dashboard-local id (provenance for the caller)
    vital_sign: str | None = None
    measure_group: str | None = None


@dataclass
class InitReport:
    """Categorised summary of an init / resync run. Never raises on unresolved rows."""
    processed: int = 0
    resolved: int = 0
    unresolved: list = field(default_factory=list)        # SeedRow that didn't resolve here
    unit_mismatch: list = field(default_factory=list)     # (SeedRow, instance_unit)
    unseeded: list = field(default_factory=list)          # triples in instance, absent from seed

    def summary(self) -> str:
        return (
            f"measure-mapping init: processed={self.processed} resolved={self.resolved} "
            f"unresolved={len(self.unresolved)} unit_mismatch={len(self.unit_mismatch)} "
            f"unseeded={len(self.unseeded)}"
        )
