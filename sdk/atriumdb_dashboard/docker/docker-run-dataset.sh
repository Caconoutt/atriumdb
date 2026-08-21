#!/usr/bin/env bash
# docker-run-dataset.sh — run the AtriumDB SDK container with a real dataset.
#
# EDIT THE LINE BELOW — set this to the absolute path of your dataset on your Mac.
# The folder must contain meta/index.db and a tsc/ subdirectory.
HOST_DATASET_PATH="/absolute/path/to/your/dataset"

# ---------------------------------------------------------------------------
# Nothing below this line should need editing.
# ---------------------------------------------------------------------------

set -euo pipefail

IMAGE="atriumdb-sdk"
CONTAINER_DATASET="/data/atriumdb"

# Validate that the user has set the path.
if [[ "$HOST_DATASET_PATH" == "/absolute/path/to/your/dataset" || -z "$HOST_DATASET_PATH" ]]; then
    echo "ERROR: HOST_DATASET_PATH is still the placeholder value." >&2
    echo "       Open docker-run-dataset.sh and set it to your real dataset path." >&2
    exit 1
fi

# Confirm the path exists on the host.
if [[ ! -d "$HOST_DATASET_PATH" ]]; then
    echo "ERROR: dataset directory not found: $HOST_DATASET_PATH" >&2
    exit 1
fi

# Check the Docker image has been built.
if ! docker image inspect "$IMAGE" > /dev/null 2>&1; then
    echo "ERROR: Docker image '$IMAGE' not found." >&2
    echo "       Build it first from the sdk/ directory:" >&2
    echo "         docker build -t $IMAGE -f atriumdb_dashboard/docker/Dockerfile ." >&2
    exit 1
fi

# Default to an interactive bash shell; pass any extra arguments to override.
CMD=("bash")
if [[ $# -gt 0 ]]; then
    CMD=("$@")
fi

echo "Dataset : $HOST_DATASET_PATH"
echo "Mounted : $CONTAINER_DATASET"
echo "Command : ${CMD[*]}"
echo ""

exec docker run --rm -it \
    -v "$HOST_DATASET_PATH:$CONTAINER_DATASET" \
    -v "$(pwd):/sdk" \
    -e "ATRIUMDB_DATASET_LOCATION=$CONTAINER_DATASET" \
    "$IMAGE" \
    "${CMD[@]}"

# # Commands on direct peeking meta tables (after enter imgae)
# python -c "
# import sqlite3
# conn = sqlite3.connect('/data/atriumdb/meta/index.db')

# rows = conn.execute('''
#     SELECT p.mrn, p.id, e.start_time, e.start_time - p.dob
#     FROM encounter e
#     JOIN patient p ON e.patient_id = p.id
#     WHERE e.start_time >= 1577836800000000000
#       AND e.start_time <= 1609459200000000000
# ''').fetchall()
# print('row count:', len(rows))
# for r in rows:
#     print(r)
# "
# # 
# python -c "
# import sqlite3
# conn = sqlite3.connect('/data/atriumdb/meta/index.db')

# # Age bounds in epoch nanoseconds (adjust years as needed)
# ONE_YEAR_NS = 365 * 24 * 3600 * 1_000_000_000
# ONE_MONTH_NS: int = 30 * 24 * 3600 * 1_000_000_000
# age_min_ns = 2 * ONE_YEAR_NS + 5 * ONE_MONTH_NS
# age_max_ns = 7 * ONE_YEAR_NS + 11 * ONE_MONTH_NS

# rows = conn.execute('''
#     SELECT p.*, e.start_time
#     FROM encounter e
#     JOIN patient p ON e.patient_id = p.id
#     WHERE e.start_time >= 1577836800000000000
#       AND e.start_time <= 1609459200000000000
#       AND p.gender = ?
#       AND p.dob IS NOT NULL
#       AND (e.start_time - p.dob) >= ?
#       AND (e.start_time - p.dob) <= ?
# ''', ('F', age_min_ns, age_max_ns)).fetchall()
# print('row count:', len(rows))
# for r in rows:
#     print(r)
# "