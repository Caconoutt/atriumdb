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
import threading
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

#: Serialises use of the API-mode SDK's single websocket.
#:
#: ``AtriumSDK._block_websocket_request`` sends a block-id list on
#: ``self.websock_conn`` and then reads from that same connection until it sees
#: ``Atriumdb_Done``. There is no lock anywhere in ``atrium_sdk.py``, and the
#: connection is deliberately held open for the life of the SDK object — which,
#: coming from ``get_data_sdk``, is process-wide and shared across requests.
#:
#: The endpoints run in FastAPI's threadpool, so two requests can reach
#: :func:`fetch_nan_filled_window` at once. Without this lock they interleave on
#: one socket: both send, then both consume from the same stream, and one can
#: return the other's blocks. Nothing raises — the bytes decode cleanly and the
#: values belong to another patient.
#:
#: Lives here rather than in ``api.dependencies`` because this module is its only
#: user and the dependency must run one way: ``api`` imports ``pipeline``, never
#: the reverse.
data_sdk_lock = threading.Lock()

#: Timeout for the block-list request in :func:`fetch_nan_filled_window`.
#:
#: No HTTP call in ``atrium_sdk.py`` sets one — a grep for ``timeout`` there
#: returns nothing — so a UAT server that accepts a connection and then stops
#: responding would hang a worker thread indefinitely. ``_request`` forwards
#: ``**kwargs`` to ``requests.request``, so the one call the dashboard makes
#: itself can be bounded even though the SDK's internal ones cannot.
_BLOCK_LIST_TIMEOUT_S = 60


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

    Uses ``get_mrn_to_patient_id_map`` rather than ``get_patient_id`` per MRN,
    because the two behave differently in api mode and only the former is
    correct here. ``get_patient_id`` is ``self._request(...)['id']``, and
    ``_request`` raises ``ValueError`` on any non-200 — so a single unknown MRN
    would abort the whole request as a 422, rather than excluding one patient
    and processing the rest. ``get_mrn_to_patient_id_map`` catches that per MRN
    and omits it, matching the direct-DB behaviour this function was written
    against. It is also what ``cohort_resolver`` already uses for the 1A path.

    One consequence to be aware of when reading a response: the SDK's ``except
    ValueError`` there cannot distinguish "404, no such MRN" from a server fault
    or an expired token, because the status survives only inside the exception
    message. A transient API problem therefore looks like a set of unknown MRNs.
    The caller logs how many were dropped (see ``_resolve_cohort_patients``) so
    that a suspicious count is diagnosable.

    :param sdk: AtriumSDK instance, direct-DB or api mode.
    :param cohort: The cohort whose patients to resolve.
    :return: Mapping of MRN to patient ID, containing only MRNs that resolved.
        ``len()`` of it is the cohort's distinct patient count.
    """
    mrn_list = [patient.mrn for patient in cohort.patients]
    if not mrn_list:
        return {}

    resolved = sdk.get_mrn_to_patient_id_map(mrn_list=mrn_list)

    # Normalise to the request's own MRN strings. The SDK keys the map by
    # ``str(mrn)``, and every caller looks the result up by ``patient.mrn``, so
    # anything that did not round-trip identically would silently drop a patient.
    return {
        patient.mrn: int(resolved[patient.mrn])
        for patient in cohort.patients
        if patient.mrn in resolved
    }


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
# Window fetch — the NaN-filled sample grid both endpoints reduce
# ---------------------------------------------------------------------------

def fetch_nan_filled_window(
    sdk: "AtriumSDK",
    measure_id: int,
    patient_id: int,
    window_start_ns: int,
    window_end_ns: int,
) -> np.ndarray:
    """Return the window as a regular grid, gaps filled with NaN, in either mode.

    Both endpoints need one slot per sample the measure's frequency implies over
    the window, NaN wherever no sample exists — for a 1 Hz measure over an hour,
    a 3600-element array however much data is actually there. Availability is the
    non-NaN fraction of it, and S3's buckets are fixed slices of it.

    **Why this exists.** ``sdk.get_data(..., return_nan_filled=True)`` produces
    that grid in direct-DB mode but is silently ignored in api mode: the api
    branch of ``get_data`` calls ``_get_data_api`` without forwarding
    ``return_nan_filled``, so it returns an unfilled 3-tuple where the caller
    unpacks two. The immediate symptom is ``ValueError: too many values to
    unpack``; the dangerous one is that, unpacked successfully, availability
    would compute as 1.0 for every window and the threshold would stop excluding
    anything.

    **Why it is not rebuilt in numpy.** NaN-filling is not post-processing on
    ``(times, values)``. It happens inside the C library, in
    ``Block.decode_blocks(..., return_nan_gap=True)``, which needs the block
    headers and the *raw, pre-analog-scaling* value buffer because it applies
    each block's own scale factors while scattering samples onto the grid. It
    also derives ``period_ns`` from the headers itself. Reconstructing that from
    a normal ``get_data`` return would mean re-deriving the scaling and rounding
    rules, and drifting from the direct-DB path.

    So the api path here restates ``AtriumSDK._get_data_api`` with the three
    arguments it drops — ``return_nan_gap``, ``start_time_n``, ``end_time_n`` —
    forwarded to the same ``decode_blocks`` call the SDK itself makes. Results
    are therefore identical to the direct-DB path rather than merely equivalent.

    Restating those few lines keeps ``sdk/atriumdb/`` byte-identical to upstream,
    which is the same trade ``cohort_resolver._post_cohorts_remote`` makes. The
    upstream functions shadowed here are ``AtriumSDK._get_data_api`` and the
    zero-block branch of ``AtriumSDK.get_data``; grep for those names when
    upgrading the SDK.

    :param sdk: AtriumSDK instance, direct-DB or api mode.
    :param measure_id: The measure to read.
    :param patient_id: The patient to read for.
    :param window_start_ns: Window start, epoch nanoseconds, inclusive.
    :param window_end_ns: Window end, epoch nanoseconds, exclusive.
    :return: 1D float64 array spanning the window at the measure's nominal
        period, NaN where no sample exists. Never ``None``; an empty window
        yields an all-NaN array of the expected length.
    """
    if getattr(sdk, "mode", None) != "api":
        _, values = sdk.get_data(
            measure_id=measure_id,
            patient_id=patient_id,
            start_time_n=window_start_ns,
            end_time_n=window_end_ns,
            return_nan_filled=True,
        )
        if values is None:
            return _all_nan_window(sdk, measure_id, window_start_ns, window_end_ns)
        return values

    params = {
        "start_time": window_start_ns,
        "end_time": window_end_ns,
        "measure_id": measure_id,
        "device_id": None,
        "patient_id": patient_id,
        "mrn": None,
    }

    # One lock across the block-list request and the websocket transfer, not
    # just the transfer: the SDK connects the websocket lazily inside
    # ``_block_websocket_request``, so a narrower lock would still let two
    # threads race to create it. See :data:`data_sdk_lock` for why the shared
    # connection cannot be used concurrently at all.
    with data_sdk_lock:
        block_info_list = sdk._request(
            "GET", "sdk/blocks", params=params, timeout=_BLOCK_LIST_TIMEOUT_S
        )

        if len(block_info_list) == 0:
            # ``decode_blocks`` cannot be handed zero blocks — it reads
            # ``headers[0]`` to derive the period. Mirrors the zero-block branch
            # of ``AtriumSDK.get_data``.
            return _all_nan_window(sdk, measure_id, window_start_ns, window_end_ns)

        num_bytes_list = [row["num_bytes"] for row in block_info_list]
        encoded_bytes = sdk._block_websocket_request(block_info_list)

        _, values = sdk.block.decode_blocks(
            encoded_bytes,
            num_bytes_list,
            analog=True,
            time_type=1,
            return_nan_gap=True,
            start_time_n=window_start_ns,
            end_time_n=window_end_ns,
        )

    return values


def _all_nan_window(
    sdk: "AtriumSDK",
    measure_id: int,
    window_start_ns: int,
    window_end_ns: int,
) -> np.ndarray:
    """Return an all-NaN grid of the length the window implies.

    The "no data at all" case, which cannot go through ``decode_blocks``. The
    arithmetic mirrors the SDK's own zero-block branch so that an empty window
    and a sparse one produce grids of the same length, which is what lets the
    caller treat availability as a pure non-NaN fraction either way.
    """
    info = sdk.get_measure_info(measure_id)
    period_ns = (info or {}).get("period_ns")
    if not period_ns:
        freq_nhz = (info or {}).get("freq_nhz")
        if not freq_nhz:
            raise ValueError(
                f"Measure {measure_id} has neither period_ns nor a non-zero freq_nhz, "
                f"so an empty window has no length."
            )
        period_ns = (10 ** 18) / freq_nhz

    expected_num_values = int(round((window_end_ns - window_start_ns) / period_ns))
    return np.full(max(0, expected_num_values), np.nan, dtype=np.float64)


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

    This is the per-patient results-table lookup, **not** the demographic cohort
    filter — that one is Priority 1B in ``cohort_resolver`` and runs on raw SQL
    against the metadata store.

    Only ``gender`` and ``dob`` are read. Over the API this record also carries
    ``mrn`` and the patient's name fields; nothing here reads or forwards them,
    and nothing logs the record itself. Keep it that way — a debug line dumping
    ``info`` would put patient names into the container log, which the direct-DB
    path never did.
    """
    try:
        info = sdk.get_patient_info(patient_id=patient_id, time=admission_ns)
    except ValueError as exc:
        # In api mode a patient the server does not know 404s, and ``_request``
        # turns any non-200 into a ValueError rather than returning None. This
        # function is best-effort by contract — demographics only decorate a
        # results-table row and never decide inclusion — so a lookup failure
        # must degrade to "unknown", exactly as a dataset without the fields
        # does, rather than failing the whole request.
        _LOGGER.debug(
            "[%s] mrn=%s: get_patient_info failed at admission_ns=%d (%s).",
            request_id, mrn, admission_ns, exc,
        )
        return None, None

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
