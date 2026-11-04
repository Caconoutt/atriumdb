"""Unit tests for ``pipeline.resolve_window_anchor``.

The API suites exercise the anchor only through datasets whose encounters were
built from ``device_patient`` (``deploy/prepare_dataset.py``), so an encounter
always starts exactly when a mapping does. Real ADT data does not line up that
way: the monitor is often connected before the admission is registered. These
tests pin the anchor's behaviour with a stub SDK so that case is covered.
"""

import numpy as np

from atriumdb_dashboard.pipeline import resolve_window_anchor
from atriumdb_dashboard.schemas import ALL_TIME, Admission

H = 3600 * 10**9
MIN = 60 * 10**9


class StubSDK:
    """Answers ``get_interval_array`` from one contiguous span of data."""

    def __init__(self, data_start: int, data_end: int):
        self.data = (data_start, data_end)
        self.calls: list[tuple[int, int | None]] = []

    def get_interval_array(self, measure_id, patient_id, start, end):
        self.calls.append((start, end))
        lo, hi = self.data
        e = end if end is not None else 10**30
        if lo < e and hi > start:
            return np.array([[max(lo, start), min(hi, e)]])
        return np.zeros((0, 2))


ADMISSION = Admission(admission_ns=10 * H, discharge_ns=100 * H)


def anchor(sdk, mappings, window=24 * H, admission=ADMISSION):
    return resolve_window_anchor(
        sdk=sdk,
        measure_id=1,
        patient_id=1,
        admission=admission,
        mappings=mappings,
        observation_window=window,
    )


def test_mapping_starting_before_admission_is_clipped_to_admission():
    # Monitor connected 5 min before the ADT admission, data throughout.
    sdk = StubSDK(10 * H - 5 * MIN, 90 * H)
    assert anchor(sdk, [(7, 10 * H - 5 * MIN, 90 * H)]) == (10 * H, 7)
    # The data check itself starts at the admission, not before it.
    assert sdk.calls[0][0] == 10 * H


def test_open_mapping_spanning_admission_is_a_candidate():
    sdk = StubSDK(0, 200 * H)
    assert anchor(sdk, [(7, 2 * H, None)]) == (10 * H, 7)


def test_mapping_that_ended_before_admission_is_skipped():
    sdk = StubSDK(0, 200 * H)
    assert anchor(sdk, [(6, 1 * H, 10 * H), (7, 12 * H, None)]) == (12 * H, 7)


def test_mapping_starting_within_admission_anchors_at_its_start():
    sdk = StubSDK(15 * H, 90 * H)
    assert anchor(sdk, [(7, 15 * H, 90 * H)]) == (15 * H, 7)


def test_mapping_after_discharge_is_not_used():
    sdk = StubSDK(0, 200 * H)
    assert anchor(sdk, [(7, 101 * H, None)]) is None


def test_no_data_in_first_window_gives_none():
    # Mapping covers the stay but the measure's data starts 30 h in.
    sdk = StubSDK(40 * H, 90 * H)
    assert anchor(sdk, [(7, 10 * H, 90 * H)]) is None


def test_all_time_overlapping_mapping_checks_up_to_discharge():
    sdk = StubSDK(50 * H, 90 * H)
    assert anchor(sdk, [(7, 5 * H, None)], window=ALL_TIME) == (10 * H, 7)
    assert sdk.calls[0] == (10 * H, 100 * H)
