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

"""Direct-DB cohort resolution logic for Priority 1A and 1B.

This module contains no HTTP awareness — it always assumes the supplied SDK
instance is connected directly to the database (``metadata_connection_type``
of ``"sqlite"``, ``"mysql"``, or ``"mariadb"``). It is called:

- directly, when the SDK caller is in direct-DB mode, and
- by the FastAPI endpoint (``cohort_endpoints.py``), when a remote caller
  sends a ``POST /cohorts`` request to the dashboard server.

The entry point is :func:`resolve_cohorts_local`, which dispatches to either
:func:`_resolve_mrn_cohort` (1A) or :func:`_resolve_demographic_cohort` (1B)
depending on the request type.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from atriumdb.dashboard.encounter_queries import (
    group_encounters_by_visit,
    query_patient_encounters,
)
from atriumdb.dashboard.schemas import (
    AdmissionDateRange,
    CohortDefinitionRequest,
    DemographicCohort,
    MrnCohort,
    MrnCohortResponse,
    ResolvedCohort,
)

if TYPE_CHECKING:
    from atriumdb import AtriumSDK

logger = logging.getLogger(__name__)


def resolve_cohorts_local(
    sdk: "AtriumSDK",
    request: CohortDefinitionRequest,
    request_id: str = "",
) -> MrnCohortResponse:
    """Resolve all cohorts in a request using direct database access.

    Dispatches each cohort to either the MRN-validation path (1A) or the
    demographic-filter path (1B) based on ``request.type``, then assembles
    the results into a :class:`~atriumdb.dashboard.schemas.MrnCohortResponse`.

    Cohorts are processed in order and results preserve that order. A cohort
    that produces zero qualifying MRNs is still included in the response with
    an empty ``mrnList``.

    :param sdk: AtriumSDK instance in direct-DB mode. Must have a live
        ``sql_handler`` — this function will raise if called with an API-mode
        SDK.
    :param request: Parsed request body containing the cohort definitions and
        the shared ``admissionDateRange``.
    :param request_id: Value of the ``X-Request-ID`` header, echoed back in
        the response and attached to log messages.
    :return: :class:`~atriumdb.dashboard.schemas.MrnCohortResponse` with one
        resolved cohort per input cohort.
    """
    resolved: list[ResolvedCohort] = []

    if request.type == "mrn":
        for cohort in request.cohorts:
            mrns = _resolve_mrn_cohort(
                sdk, cohort, request.admissionDateRange, request_id
            )
            resolved.append(ResolvedCohort(id=cohort.id, mrnList=mrns))

    elif request.type == "demographic":
        for cohort in request.cohorts:
            mrns = _resolve_demographic_cohort(
                sdk, cohort, request.admissionDateRange
            )
            resolved.append(ResolvedCohort(id=cohort.id, mrnList=mrns))

    return MrnCohortResponse(requestId=request_id, cohorts=resolved)


def _resolve_mrn_cohort(
    sdk: "AtriumSDK",
    cohort: MrnCohort,
    admission_date_range: AdmissionDateRange,
    request_id: str,
) -> list[str]:
    """Validate an explicit MRN list against AtriumDB (Priority 1A).

    Applies two checks in sequence, logging and silently dropping failures at
    each stage:

    1. **Existence check** — does the MRN exist in the ``patient`` table?
       Uses ``sdk.get_mrn_to_patient_id_map`` which returns only found MRNs.
    2. **Admission range check** — does the patient have at least one
       ``encounter`` record with ``start_time`` within ``admission_date_range``?
       Uses :func:`~atriumdb.dashboard.encounter_queries.query_patient_encounters`
       with no location filter (any unit counts).

    MRNs are normalised by stripping surrounding whitespace before either
    check (design doc Assumption §5). No further normalisation is performed —
    comparison is exact string match.

    :param sdk: AtriumSDK instance in direct-DB mode.
    :param cohort: The :class:`~atriumdb.dashboard.schemas.MrnCohort` to
        validate, containing the ``id`` and ``mrnList``.
    :param admission_date_range: Inclusive window (epoch nanoseconds). An MRN
        must have an encounter with ``start_time`` in ``[start, end]``.
    :param request_id: Attached to log messages to allow correlation across
        log lines for the same request.
    :return: List of MRNs that passed both checks, in arbitrary order. May be
        empty if no MRNs in the input passed.
    """
    # Step 0 — normalise (trim whitespace per Assumption §5)
    mrn_input = [m.strip() for m in cohort.mrnList]

    # Step 1 — existence check
    mrn_to_pid: dict[str, int] = sdk.get_mrn_to_patient_id_map(mrn_list=mrn_input)

    recognised = [m for m in mrn_input if m in mrn_to_pid]
    unrecognised = [m for m in mrn_input if m not in mrn_to_pid]

    if unrecognised:
        logger.warning(
            "MRNs not found in AtriumDB — excluded from cohort",
            extra={
                "request_id": request_id,
                "cohort_id": cohort.id,
                "mrns": unrecognised,
            },
        )

    if not recognised:
        return []

    # Step 2 — admission range check (no location filter: any unit qualifies)
    patient_id_to_mrn = {
        pid: mrn for mrn, pid in mrn_to_pid.items() if mrn in recognised
    }

    encounter_rows = query_patient_encounters(
        sdk,
        patient_id_list=list(patient_id_to_mrn.keys()),
        admit_start_ns=admission_date_range.start,
        admit_end_ns=admission_date_range.end,
    )

    visits = group_encounters_by_visit(encounter_rows)
    patient_ids_with_admission = {pid for pid, _vn in visits}
    mrns_with_admission = {
        patient_id_to_mrn[pid] for pid in patient_ids_with_admission
    }

    no_admission_in_range = [m for m in recognised if m not in mrns_with_admission]
    if no_admission_in_range:
        logger.warning(
            "MRNs exist in AtriumDB but have no encounter record in the "
            "requested date range — excluded from cohort",
            extra={
                "request_id": request_id,
                "cohort_id": cohort.id,
                "mrns": no_admission_in_range,
                "admit_start_ns": admission_date_range.start,
                "admit_end_ns": admission_date_range.end,
            },
        )

    return list(mrns_with_admission)


def _resolve_demographic_cohort(
    sdk: "AtriumSDK",
    cohort: DemographicCohort,
    admission_date_range: AdmissionDateRange,
) -> list[str]:
    """Resolve a demographic cohort filter to a validated MRN list (Priority 1B).

    Applies three stages:

    **Stage 1 — Location + date filter (SQL)**
        Calls :func:`~atriumdb.dashboard.encounter_queries.query_patient_encounters`
        with ``locations`` and ``admissionDateRange``. This constrains
        candidates to patients who were admitted to the specified unit type
        *during* the date range of interest — not patients who were ever
        admitted there. Then collapses rows to per-visit records via
        :func:`~atriumdb.dashboard.encounter_queries.group_encounters_by_visit` and
        selects each patient's **reference admission**: the earliest in-range
        visit per design doc Assumption §4.

    **Stage 2 — Demographics fetch (SQL)**
        Calls ``sdk.sql_handler.select_all_patients_in_list`` with the
        candidate patient IDs from Stage 1. Returns ``(id, mrn, gender, dob,
        ...)`` tuples (column order from ``maria_handler.py:703``).

    **Stage 3 — Age and sex filters (Python)**
        Age is evaluated at each patient's reference ``admit_time_ns`` from
        Stage 1 (not the current date)::

            age_at_admission_ns = admit_time_ns - dob_ns

        Age bounds arrive as nanoseconds pre-converted by the dashboard server.
        Patients with ``dob = None`` are excluded when an age filter is
        supplied. Sex ``"U"`` matches NULL, empty, or the literal ``'U'``
        stored in ``patient.gender``.

    All active filters are AND-ed; within each filter the individual values
    are OR-ed (e.g. ``sex=["F","U"]`` keeps female *or* unknown patients).

    :param sdk: AtriumSDK instance in direct-DB mode.
    :param cohort: The :class:`~atriumdb.dashboard.schemas.DemographicCohort`
        containing ``age``, ``sex``, and ``location`` filters.
    :param admission_date_range: Inclusive window (epoch nanoseconds) used
        both to scope the encounter candidate pool and to anchor each
        patient's age-at-admission calculation.
    :return: List of MRNs that passed all active filters, in arbitrary order.
        May be empty if no patients matched.
    """
    # Stage 1 — location + date filter
    encounter_rows = query_patient_encounters(
        sdk,
        locations=cohort.location,
        admit_start_ns=admission_date_range.start,
        admit_end_ns=admission_date_range.end,
    )

    visits = group_encounters_by_visit(encounter_rows)

    # Reference admission = earliest admit_time_ns per patient
    reference_admission: dict[int, dict] = {}
    for (pid, _vn), visit in visits.items():
        if pid not in reference_admission or (
            visit["admit_time_ns"] < reference_admission[pid]["admit_time_ns"]
        ):
            reference_admission[pid] = visit

    if not reference_admission:
        return []

    # Stage 2 — fetch demographics for the candidate patient set
    patient_rows = sdk.sql_handler.select_all_patients_in_list(
        patient_id_list=list(reference_admission.keys())
    )
    # Column order from maria_handler.py:703:
    # id, mrn, gender, dob, first_name, middle_name, last_name,
    # first_seen, last_updated, source_id, weight, height
    demographics: dict[int, dict] = {
        row[0]: {"mrn": row[1], "gender": row[2], "dob_ns": row[3]}
        for row in patient_rows
    }

    # Stage 3 — age and sex filters (pure Python)
    surviving_mrns: list[str] = []

    for pid, visit in reference_admission.items():
        demo = demographics.get(pid, {})
        mrn = demo.get("mrn")
        dob_ns = demo.get("dob_ns")
        admit_time_ns = visit["admit_time_ns"]

        # Age filter: evaluated at admit_time_ns, not the current date
        age_ok = True
        if cohort.age:
            if dob_ns is None:
                age_ok = False  # unknown dob — cannot verify age
            else:
                age_at_admission_ns = admit_time_ns - dob_ns
                age_ok = any(
                    band.startNs <= age_at_admission_ns <= band.endNs
                    for band in cohort.age
                )

        # Sex filter: "U" matches NULL / empty / literal 'U'
        sex_ok = True
        if cohort.sex:
            requested = {s.upper() for s in cohort.sex}
            gender_raw = demo.get("gender")
            if gender_raw is None or gender_raw.strip() == "":
                sex_ok = "U" in requested
            else:
                g = gender_raw.strip().upper()
                sex_ok = g in requested or (g == "U" and "U" in requested)

        if age_ok and sex_ok and mrn is not None:
            surviving_mrns.append(mrn)

    return surviving_mrns
