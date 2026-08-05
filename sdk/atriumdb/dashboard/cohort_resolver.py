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
    group_encounters_by_admission,
    query_patient_encounters,
)
from atriumdb.dashboard.schemas import (
    Admission,
    AdmissionDateRange,
    CohortDefinitionRequest,
    DemographicCohort,
    MrnCohort,
    MrnCohortResponse,
    PatientAdmission,
    ResolvedCohort,
)

if TYPE_CHECKING:
    from atriumdb import AtriumSDK

_LOGGER = logging.getLogger(__name__)


def _to_admission(record: dict) -> Admission:
    """Build the response ``Admission`` from a grouped encounter record.

    The admission and discharge times and the unit name all fall out of the
    encounter join performed during filtering, so nothing is re-queried here.
    """
    return Admission(
        admission_ns=record["admit_time_ns"],
        discharge_ns=record["discharge_time_ns"],
        location=record["unit_name"],
    )


def resolve_cohorts_local(
    sdk: "AtriumSDK",
    request: CohortDefinitionRequest,
    request_id: str,
) -> MrnCohortResponse:
    """Resolve all cohorts in a request using direct database access.

    Dispatches each cohort to either the MRN-validation path (1A) or the
    demographic-filter path (1B) based on ``request.type``, then assembles
    the results into a :class:`~atriumdb.dashboard.schemas.MrnCohortResponse`.

    Cohorts are processed in order and results preserve that order. A cohort
    that produces zero qualifying patients is still included in the response
    with an empty ``patients`` list.

    :param sdk: AtriumSDK instance in direct-DB mode. Must have a live
        ``sql_handler`` — this function will raise if called with an API-mode
        SDK.
    :param request: Parsed request body containing the cohort definitions and
        the shared ``admissionDateRange``.
    :param request_id: Value of the ``X-Request-ID`` header, echoed back in
        the response and attached to log messages. Caller guarantees this is a
        non-empty string (validated by :meth:`~atriumdb.AtriumSDK.dashboard_resolve_cohort`).
    :return: :class:`~atriumdb.dashboard.schemas.MrnCohortResponse` with one
        resolved cohort per input cohort.
    """
    resolved: list[ResolvedCohort] = []

    if request.type == "mrn":
        for cohort in request.cohorts:
            patients = _resolve_mrn_cohort(
                sdk, cohort, request.admission_date_range, request_id
            )
            resolved.append(ResolvedCohort(id=cohort.id, patients=patients))

    elif request.type == "demographic":
        for cohort in request.cohorts:
            patients = _resolve_demographic_cohort(
                sdk, cohort, request.admission_date_range, request_id
            )
            resolved.append(ResolvedCohort(id=cohort.id, patients=patients))

    return MrnCohortResponse(request_id=request_id, cohorts=resolved)


def _resolve_mrn_cohort(
    sdk: "AtriumSDK",
    cohort: MrnCohort,
    admission_date_range: AdmissionDateRange,
    request_id: str,
) -> list[PatientAdmission]:
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
    :return: List of :class:`~atriumdb.dashboard.schemas.PatientAdmission` for
        MRNs that passed both checks. Each entry contains all qualifying
        admission timestamps (sorted ascending) for that patient within the
        date range. May be empty if no MRNs in the input passed.
    """
    # Step 0 — normalise (trim whitespace per Assumption §5)
    mrn_input = [m.strip() for m in cohort.mrn_list]

    # Step 1 — existence check
    mrn_to_pid: dict[str, int] = sdk.get_mrn_to_patient_id_map(mrn_list=mrn_input)

    recognised = [m for m in mrn_input if m in mrn_to_pid]
    unrecognised = [m for m in mrn_input if m not in mrn_to_pid]

    if unrecognised:
        _LOGGER.warning(
            "[%s] MRN Cohort Definition — cohort %r: MRNs not found in AtriumDB: %s",
            request_id,
            cohort.id,
            unrecognised,
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

    admission_records = group_encounters_by_admission(encounter_rows)

    # Collect all qualifying admissions per patient, each carrying its location
    patient_admissions: dict[int, list[Admission]] = {}
    for record in admission_records.values():
        patient_admissions.setdefault(record["patient_id"], []).append(
            _to_admission(record)
        )

    mrns_with_admission = {
        patient_id_to_mrn[pid] for pid in patient_admissions
    }

    no_admission_in_range = [m for m in recognised if m not in mrns_with_admission]
    if no_admission_in_range:
        _LOGGER.warning(
            "[%s] MRN Cohort Definition — cohort %r: MRNs with no encounter in "
            "date range [%s, %s]: %s",
            request_id,
            cohort.id,
            admission_date_range.start,
            admission_date_range.end,
            no_admission_in_range,
        )

    return [
        PatientAdmission(
            mrn=patient_id_to_mrn[pid],
            admissions=sorted(admissions, key=lambda a: a.admission_ns),
        )
        for pid, admissions in patient_admissions.items()
    ]


def _resolve_demographic_cohort(
    sdk: "AtriumSDK",
    cohort: DemographicCohort,
    admission_date_range: AdmissionDateRange,
    request_id: str,
) -> list[PatientAdmission]:
    """Resolve a demographic cohort filter to a validated patient list (Priority 1B).

    Applies three stages:

    **Stage 1 — Location + date filter (SQL)**
        Calls :func:`~atriumdb.dashboard.encounter_queries.query_patient_encounters`
        with ``locations`` and ``admissionDateRange``. Collapses rows to
        per-admission records via
        :func:`~atriumdb.dashboard.encounter_queries.group_encounters_by_admission`.
        All qualifying admissions per patient are retained — not just the earliest.

    **Stage 2 — Demographics fetch (SQL)**
        Calls ``sdk.sql_handler.select_all_patients_in_list`` with the
        candidate patient IDs from Stage 1. Returns ``(id, mrn, gender, dob,
        ...)`` tuples (column order from ``maria_handler.py:703``).

    **Stage 3 — Age and sex filters (Python)**
        Sex is a patient-level filter applied once per patient. Age is
        evaluated per visit at each visit's ``admit_time_ns`` (not the current
        date)::

            age_at_admission_ns = admit_time_ns - dob_ns

        Only visits where the age filter passes are included in the patient's
        ``admissions`` list. Patients with ``dob = None`` are excluded from
        all visits when an age filter is supplied. Sex ``"U"`` matches NULL,
        empty, or the literal ``'U'`` stored in ``patient.gender``.

    All active filters are AND-ed; within each filter the individual values
    are OR-ed (e.g. ``sex=["F","U"]`` keeps female *or* unknown patients).

    :param sdk: AtriumSDK instance in direct-DB mode.
    :param cohort: The :class:`~atriumdb.dashboard.schemas.DemographicCohort`
        containing ``age``, ``sex``, and ``location`` filters.
    :param admission_date_range: Inclusive window (epoch nanoseconds) used
        both to scope the encounter candidate pool and to anchor each
        visit's age-at-admission calculation.
    :param request_id: Attached to log messages to allow correlation across
        log lines for the same request.
    :return: List of :class:`~atriumdb.dashboard.schemas.PatientAdmission` for
        patients that passed all active filters. Each entry contains all
        qualifying visit admission timestamps (sorted ascending). May be empty
        if no patients matched.
    """
    # Stage 1 — location + date filter
    try:
        encounter_rows = query_patient_encounters(
            sdk,
            locations=cohort.location,
            admit_start_ns=admission_date_range.start,
            admit_end_ns=admission_date_range.end,
        )
    except ValueError as exc:
        _LOGGER.error(
            "[%s] Demographic Cohort Definition — cohort %r: %s",
            request_id,
            cohort.id,
            exc,
        )
        raise

    admission_records = group_encounters_by_admission(encounter_rows)

    # Collect all admissions per patient — each (visit, unit) is a distinct admission
    patient_visits: dict[int, list[dict]] = {}
    for record in admission_records.values():
        patient_visits.setdefault(record["patient_id"], []).append(record)

    if not patient_visits:
        return []

    # Stage 2 — fetch demographics for the candidate patient set
    patient_rows = sdk.sql_handler.select_all_patients_in_list(
        patient_id_list=list(patient_visits.keys())
    )
    # Column order from maria_handler.py:703:
    # id, mrn, gender, dob, first_name, middle_name, last_name,
    # first_seen, last_updated, source_id, weight, height
    demographics: dict[int, dict] = {
        row[0]: {"mrn": row[1], "gender": row[2], "dob_ns": row[3]}
        for row in patient_rows
    }

    # Stage 3 — sex filter at patient level; age filter per visit
    # A patient is included if they pass sex and have at least one visit that
    # passes the age filter. All qualifying visit admissions are returned.
    surviving_patients: list[PatientAdmission] = []
    patients_without_mrn: list[int] = []

    for pid, visit_list in patient_visits.items():
        demo = demographics.get(pid, {})
        mrn = demo.get("mrn")
        dob_ns = demo.get("dob_ns")

        if mrn is None:
            # Admitted in range but unusable downstream — the response is keyed
            # by MRN. Indicates incomplete patient data, so log rather than drop
            # silently.
            patients_without_mrn.append(pid)
            continue

        # Sex filter: patient-level, not visit-level
        sex_ok = True
        if cohort.sex:
            requested = {s.upper() for s in cohort.sex}
            gender_raw = demo.get("gender")
            if gender_raw is None or gender_raw.strip() == "":
                sex_ok = "U" in requested
            else:
                g = gender_raw.strip().upper()
                sex_ok = g in requested or (g == "U" and "U" in requested)

        if not sex_ok:
            continue

        # Age filter: evaluated per admission at each admit_time_ns
        qualifying_admissions: list[Admission] = []
        for visit in visit_list:
            admit_time_ns = visit["admit_time_ns"]
            age_ok = True
            if cohort.age:
                if dob_ns is None:
                    age_ok = False  # unknown dob — cannot verify age
                else:
                    age_at_admission_ns = admit_time_ns - dob_ns
                    age_ok = any(
                        band.start_ns <= age_at_admission_ns <= band.end_ns
                        for band in cohort.age
                    )
            if age_ok:
                qualifying_admissions.append(_to_admission(visit))

        if qualifying_admissions:
            surviving_patients.append(
                PatientAdmission(
                    mrn=str(mrn),
                    admissions=sorted(qualifying_admissions, key=lambda a: a.admission_ns),
                )
            )

    if patients_without_mrn:
        _LOGGER.warning(
            "[%s] Demographic Cohort Definition — cohort %r: patient_ids "
            "admitted in range but with no MRN on record, excluded: %s",
            request_id,
            cohort.id,
            patients_without_mrn,
        )

    return surviving_patients
