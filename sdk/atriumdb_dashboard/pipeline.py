# AtriumDB is a timeseries database software designed to best handle the unique features and
# challenges that arise from clinical waveform data.
#     Copyright (C) 2023  The Hospital for Sick Children
#
#     This program is free software: you can redistribute it and/or modify
#     it under the terms of the GNU General Public License as published by
#     the Free Software Foundation, either version 3 of the License, or
#     (at your option) any later version.
#
#     This program is distributed in the hope that it will be useful,
#     but WITHOUT ANY WARRANTY; without even the implied warranty of
#     MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#     GNU General Public License for more details.
#
#     You should have received a copy of the GNU General Public License
#     along with this program.  If not, see <https://www.gnu.org/licenses/>.

"""Pipeline stages shared by the statistics (S2) and time-series (S3) resolvers.

Both endpoints take the same cohort input and run the same opening stages —
measure resolution, MRN → patient_id resolution, observation-window computation,
value-range resolution, and demographics — before diverging on how they reduce
the signal. Those stages live here so the two resolvers cannot drift apart.

Every function is deliberately **request-model-agnostic**: it takes the scalars
it needs rather than an ``AggregateStatisticsRequest`` or a ``TimeSeriesRequest``.
The two request models are not in a subclass relationship (their
``observation_window`` types differ), so a helper typed against either one would
force the other caller to fabricate a request object it does not have.

What is deliberately *not* here:

* ``_extract_patient_mean`` — S2's whole-window reduction contains the
  window-level availability gate that S3 must not inherit. Only its value-range
  masking rule is shared, as :func:`usable_mask`.
* Exclusion-record construction — the two endpoints emit different record
  shapes (``ExclusionRecord`` keyed by MRN, ``VisitExclusion`` keyed by visit
  index), so each resolver builds and logs its own.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import numpy as np

from atriumdb_dashboard.schemas import (
    ALL_TIME,
    Admission,
    CohortInput,
    MeasureIdentifier,
    ValueRange,
    ValueRangeMap,
)

if TYPE_CHECKING:
    from atriumdb import AtriumSDK

_LOGGER = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Measure resolution
# ---------------------------------------------------------------------------

def resolve_measure_id(
    sdk: "AtriumSDK",
    measure: MeasureIdentifier,
    request_id: str,
) -> int:
    """Resolve a measure identifier to an internal measure_id; raise if not found.

    :param sdk: AtriumSDK instance in direct-DB mode.
    :param measure: The tag/freq/units triple naming the signal.
    :param request_id: Correlation ID prepended to the log lines.
    :return: The internal integer measure ID.
    :raises ValueError: If no measure in the dataset matches. Both endpoints map
        this to a 422, so the message is client-facing.
    """
    measure_id = sdk.get_measure_id(
        measure.measure_tag,
        freq=measure.freq,
        units=measure.units,
        freq_units=measure.freq_units,
    )
    if measure_id is None:
        raise ValueError(
            f"[{request_id}] Measure not found in dataset: tag='{measure.measure_tag}', "
            f"freq={measure.freq} {measure.freq_units}, units='{measure.units}'"
        )
    _LOGGER.debug(
        "[%s] Resolved measure '%s' (freq=%s %s, units=%s) -> measure_id=%d",
        request_id, measure.measure_tag, measure.freq, measure.freq_units,
        measure.units, measure_id,
    )
    return measure_id


# ---------------------------------------------------------------------------
# Patient ID resolution
# ---------------------------------------------------------------------------

def resolve_patient_ids(sdk: "AtriumSDK", cohort: CohortInput) -> dict[str, int]:
    """Return ``{mrn: patient_id}`` for every MRN in the cohort that resolves.

    MRNs that do not resolve are simply absent from the returned mapping. Each
    caller derives its own exclusion records from that difference, because the
    two endpoints key exclusions differently — S2 by MRN, S3 by visit index —
    and folding record construction in here would force one shape on both.

    :param sdk: AtriumSDK instance in direct-DB mode.
    :param cohort: The cohort whose patients to resolve.
    :return: Mapping of MRN to patient ID, containing only MRNs that resolved.
        ``len()`` of it is the cohort's distinct patient count.
    """
    resolved: dict[str, int] = {}
    for patient in cohort.patients:
        patient_id = sdk.get_patient_id(mrn=patient.mrn)
        if patient_id is None:
            continue
        resolved[patient.mrn] = patient_id
    return resolved


# ---------------------------------------------------------------------------
# Observation window
# ---------------------------------------------------------------------------

def compute_observation_window(
    admission: Admission,
    observation_window: int | str,
) -> tuple[int, int] | None:
    """Return ``(start_ns, end_ns)`` for this admission, or None if unbounded.

    A fixed window runs for ``observation_window`` nanoseconds from the
    admission. Under ``"all_time"`` it instead spans the admission itself, so
    the stay's own discharge is what ends it.

    :param admission: The admission anchoring the window.
    :param observation_window: A fixed length in nanoseconds, or
        :data:`~atriumdb_dashboard.schemas.ALL_TIME`.
    :return: The window, or ``None`` when ``"all_time"`` was requested but this
        admission has no usable discharge — an open stay, or a discharge that
        does not follow the admission. Such an entry has no window to measure
        availability against, and the caller excludes it. A fixed window always
        bounds, so a caller that never passes ``"all_time"`` (S3) never sees
        ``None``.
    """
    if observation_window != ALL_TIME:
        return admission.admission_ns, admission.admission_ns + observation_window

    if admission.discharge_ns is None or admission.discharge_ns <= admission.admission_ns:
        return None

    return admission.admission_ns, admission.discharge_ns


# ---------------------------------------------------------------------------
# Value-range resolution
# ---------------------------------------------------------------------------

def resolve_value_range(
    cohort: CohortInput,
    measure_tag: str,
    global_range: ValueRangeMap | None,
    request_id: str,
) -> ValueRange | None:
    """Return the bounds in force for this cohort, or None if the signal is unbounded.

    Both maps are keyed by measure tag, and only the tag named by the request's
    ``measure`` is ever consulted — bounds keyed by any other tag do not apply.

    When a cohort and the global request both bound the tag, the two are
    intersected rather than one replacing the other: the tighter of the two
    bounds wins at each end independently, so a value must satisfy both to
    count. An end left open (``None``) constrains nothing, so the other side's
    bound carries. When only one of the two is present it applies on its own.

    :param cohort: The cohort, carrying its own optional ``value_range``.
    :param measure_tag: The tag of the measure being analysed; the only key
        consulted in either map.
    :param global_range: The request-level bounds map, or ``None``.
    :param request_id: Correlation ID prepended to the log lines.
    :return: The intersected bounds, or ``None`` when neither end is bounded.
    """
    global_value_range = (global_range or {}).get(measure_tag)
    cohort_range = (cohort.value_range or {}).get(measure_tag)

    lowers = [
        r.lower for r in (global_value_range, cohort_range)
        if r is not None and r.lower is not None
    ]
    uppers = [
        r.upper for r in (global_value_range, cohort_range)
        if r is not None and r.upper is not None
    ]

    # Tighter bound wins at each end: the highest floor, the lowest ceiling.
    lower = max(lowers) if lowers else None
    upper = min(uppers) if uppers else None

    if lower is None and upper is None:
        _LOGGER.debug(
            "[%s] Cohort %s: no value range in force for tag '%s' — signal unbounded.",
            request_id, cohort.id, measure_tag,
        )
        return None

    _LOGGER.debug(
        "[%s] Cohort %s: value range for tag '%s' (global=%s, cohort=%s) -> lower=%s, upper=%s",
        request_id, cohort.id, measure_tag, global_value_range, cohort_range, lower, upper,
    )
    return ValueRange(lower=lower, upper=upper)


# ---------------------------------------------------------------------------
# Value-range masking
# ---------------------------------------------------------------------------

def usable_mask(values: np.ndarray, value_range: ValueRange | None) -> np.ndarray:
    """Return the boolean mask of samples that count as present.

    A sample is usable when it is not NaN and, if bounds are in force, falls
    inside them at both ends. Out-of-range samples are treated as *absent*
    rather than merely skipped: they lower the covered fraction of a window (S2)
    or of a bucket (S3), so a signal that is mostly artefact fails its
    availability threshold instead of producing a plausible-looking mean.

    Shared so that both endpoints apply byte-identical range semantics. This is
    the only part of S2's ``_extract_patient_mean`` that S3 reuses — the
    surrounding whole-window availability gate is deliberately left behind.

    :param values: 1D float array of samples, possibly containing NaN.
    :param value_range: Bounds to apply, or ``None`` for NaN-filtering only.
    :return: Boolean array the same length as ``values``.
    """
    mask = ~np.isnan(values)
    if value_range is None:
        return mask
    if value_range.lower is not None:
        mask &= values >= value_range.lower
    if value_range.upper is not None:
        mask &= values <= value_range.upper
    return mask


# ---------------------------------------------------------------------------
# Demographics — sex and age-at-admission for the results tables
# ---------------------------------------------------------------------------

def age_months(dob_ns: int, admission_ns: int) -> int | None:
    """Whole months elapsed from ``dob_ns`` to ``admission_ns`` (3y 4m -> 40).

    Counted on the calendar rather than by dividing a nanosecond span, so month
    lengths don't accumulate drift. The final month only counts once the day of
    the month is reached.

    :return: Whole months, or ``None`` when the dob falls after the admission,
        which means the record is inconsistent.
    """
    dob = datetime.fromtimestamp(dob_ns / 1e9, tz=timezone.utc)
    admitted = datetime.fromtimestamp(admission_ns / 1e9, tz=timezone.utc)

    months = (admitted.year - dob.year) * 12 + (admitted.month - dob.month)
    if admitted.day < dob.day:
        months -= 1

    # A dob after the admission means the record is inconsistent; report unknown
    # rather than a negative age.
    return months if months >= 0 else None


def fetch_demographics(
    sdk: "AtriumSDK",
    patient_id: int,
    mrn: str,
    admission_ns: int,
    request_id: str,
) -> tuple[str | None, int | None]:
    """Return ``(sex, age_months)`` as of this admission; either may be None.

    Demographics are best-effort: a dataset that does not record gender or dob
    yields ``None``, which the dashboard renders as an em-dash. Missing values
    never exclude an entry.
    """
    info = sdk.get_patient_info(patient_id=patient_id, time=admission_ns)
    if info is None:
        _LOGGER.debug(
            "[%s] mrn=%s: get_patient_info returned no record at admission_ns=%d.",
            request_id, mrn, admission_ns,
        )
        return None, None

    sex = info.get("gender") or None

    dob_ns = info.get("dob")
    age = None if dob_ns is None else age_months(dob_ns, admission_ns)

    return sex, age
