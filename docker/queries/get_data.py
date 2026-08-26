#!/usr/bin/env python3
"""Pull raw signal values through the SDK and dump them to a file.

    python queries/get_data.py

Edit the block between the two markers, nothing else. The dataset is mounted
read-only, so this can only read; the dump is written to /workspace/out, which
appears in docker/out/ on the host.

Identify the signal either by MEASURE_ID directly, or by MEASURE_TAG + MEASURE_FREQ
+ MEASURE_UNITS and let the SDK resolve it. Identify the source by exactly one of
DEVICE_ID or PATIENT_ID.
"""
import os
import sys

import numpy as np

from atriumdb import AtriumSDK

DATASET = os.environ.get("ATRIUMDB_DATASET_LOCATION", "/data/atriumdb")
OUT_DIR = os.environ.get("ATRIUMDB_OUT_DIR", "/workspace/out")

# Handy constants for epoch-nanosecond columns (dob, start_time, start_time_n, ...)
SECOND_NS = 1_000_000_000
MINUTE_NS = 60 * SECOND_NS
HOUR_NS = 3600 * SECOND_NS
DAY_NS = 24 * HOUR_NS
YEAR_2020 = 1577836800000000000
YEAR_2021 = 1609459200000000000
YEAR_2022 = 1640995200000000000
YEAR_2023 = 1672531200000000000
YEAR_2024 = 1704067200000000000
YEAR_2025 = 1735689600000000000
YEAR_2026 = 1767225600000000000


def to_ns(date_str: str) -> int:
    """'2020-01-01' or '2020-01-01 13:45:00' (UTC) -> epoch nanoseconds."""
    from datetime import datetime, timezone

    fmt = "%Y-%m-%d %H:%M:%S" if " " in date_str else "%Y-%m-%d"
    dt = datetime.strptime(date_str, fmt).replace(tzinfo=timezone.utc)
    return int(dt.timestamp()) * SECOND_NS


# ============================== EDIT BELOW ==============================

# --- which signal: either MEASURE_ID, or the tag/freq/units triple ---
MEASURE_ID = 11                              # set this to skip the lookup
MEASURE_TAG = "MDC_ECG_CARD_BEAT_RATE"
MEASURE_FREQ = 976562500                       # in MEASURE_FREQ_UNITS
MEASURE_UNITS = "nHz"                          # the measure table's unit column
MEASURE_FREQ_UNITS = None                      # None -> the SDK's default, nHz

# --- which source: set exactly ONE of these ---
DEVICE_ID = None
PATIENT_ID = 10002

# --- which window ---
START_NS = 1632849216000000000
END_NS = START_NS + 24 * HOUR_NS              # or: to_ns("2020-01-01 13:45:00")

# --- how to fetch ---
# False -> get_data returns (headers, times, values): real timestamps, gaps absent.
# True  -> get_data returns (headers, values): a regular grid over the whole
#          window with gaps filled as NaN, and NO times array. This is the mode
#          the dashboard's time-series resolver uses.
NAN_FILLED = True

# --- output ---
OUT_NAME = "get_data.txt"
LIMIT_PRINT = 1500                                # rows echoed to stdout
MAX_ROWS = None                                # cap rows written; None = all

# ============================== EDIT ABOVE ==============================


def resolve_measure_id(sdk) -> int:
    if MEASURE_ID is not None:
        return MEASURE_ID

    measure_id = sdk.get_measure_id(
        measure_tag=MEASURE_TAG,
        freq=MEASURE_FREQ,
        units=MEASURE_UNITS,
        freq_units=MEASURE_FREQ_UNITS,
    )
    if measure_id is None:
        sys.exit(
            f"No measure matches tag={MEASURE_TAG!r} freq={MEASURE_FREQ} "
            f"units={MEASURE_UNITS!r}. List what exists with:\n"
            f"  python -c \"from atriumdb import AtriumSDK; "
            f"sdk=AtriumSDK(dataset_location='{DATASET}'); "
            f"print([(i, m['tag'], m['freq_nhz'], m['unit']) "
            f"for i, m in sdk.get_all_measures().items()][:20])\""
        )
    return measure_id


def resolve_source() -> dict:
    """Return the kwarg naming the data source, rejecting an ambiguous pair."""
    if (DEVICE_ID is None) == (PATIENT_ID is None):
        sys.exit("Set exactly one of DEVICE_ID or PATIENT_ID, not both or neither.")
    return {"device_id": DEVICE_ID} if DEVICE_ID is not None else {"patient_id": PATIENT_ID}


def main() -> None:
    sdk = AtriumSDK(dataset_location=DATASET)

    measure_id = resolve_measure_id(sdk)
    source = resolve_source()

    if NAN_FILLED:
        # This mode returns a 2-tuple, not the usual 3: the grid is regular, so
        # there is nothing a times array would tell you that the index does not.
        headers, values = sdk.get_data(
            measure_id=measure_id,
            start_time_n=START_NS,
            end_time_n=END_NS,
            return_nan_filled=True,
            **source,
        )
        times = None
    else:
        headers, times, values = sdk.get_data(
            measure_id=measure_id,
            start_time_n=START_NS,
            end_time_n=END_NS,
            **source,
        )

    values = np.asarray([] if values is None else values)
    if times is not None:
        times = np.asarray([] if times is None else times)

    source_desc = ", ".join(f"{k}={v}" for k, v in source.items())
    print(f"measure_id={measure_id}  {source_desc}")
    print(f"window=[{START_NS}, {END_NS}]  ({(END_NS - START_NS) / SECOND_NS:g}s)")
    print(f"blocks={len(headers)}  samples={values.size}  nan_filled={NAN_FILLED}")

    if values.size == 0:
        print("\nNo samples returned. Common causes:")
        print("  - the window does not overlap any stored data for this source")
        print("  - PATIENT_ID has no encounter covering this window")
        print("  - the measure exists but was never recorded for this device")
    else:
        n_nan = int(np.count_nonzero(np.isnan(values)))
        print(f"nan={n_nan}  min={np.nanmin(values)}  max={np.nanmax(values)}")
        if times is not None:
            print(f"first {LIMIT_PRINT} times : {times[:LIMIT_PRINT]}")
        print(f"first {LIMIT_PRINT} values: {values[:LIMIT_PRINT]}")

    os.makedirs(OUT_DIR, exist_ok=True)
    out_path = os.path.join(OUT_DIR, OUT_NAME)
    n_rows = values.size if MAX_ROWS is None else min(MAX_ROWS, values.size)

    with open(out_path, "w") as handle:
        handle.write(f"measure_id={measure_id}\n")
        handle.write(f"{source_desc}\n")
        handle.write(f"start_ns={START_NS}\nend_ns={END_NS}\n")
        handle.write(f"nan_filled={NAN_FILLED}\n")
        handle.write(f"blocks={len(headers)}\nsamples={values.size}\n")
        handle.write(f"values_dtype={values.dtype}\n")
        if times is not None:
            handle.write(f"times_dtype={times.dtype}\n")
        handle.write("\n")

        # One row per sample rather than printing the arrays: numpy summarises
        # anything past 1000 elements ("[0. 1. 2. ... 86398. 86399.]"), so a
        # printed array is a preview, not the data.
        if times is not None:
            handle.write("time_ns\tvalue\n")
            for t, v in zip(times[:n_rows], values[:n_rows]):
                handle.write(f"{int(t)}\t{v!r}\n")
        else:
            # No times in nan-filled mode; the index IS the position in the grid.
            handle.write("index\tvalue\n")
            for i in range(n_rows):
                handle.write(f"{i}\t{values[i]!r}\n")

        if n_rows < values.size:
            handle.write(f"\n... {values.size - n_rows} more rows omitted (MAX_ROWS)\n")

    print(f"\nwrote {n_rows} rows -> {out_path}")
    if n_rows < values.size:
        print(f"({values.size - n_rows} rows omitted; raise MAX_ROWS to get them all)")


if __name__ == "__main__":
    main()
