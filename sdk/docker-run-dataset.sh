#!/usr/bin/env bash
# docker-run-dataset.sh — run the AtriumDB SDK container with a real dataset.
#
# EDIT THE LINE BELOW — set this to the absolute path of your dataset on your Mac.
# The folder must contain meta/index.db and a tsc/ subdirectory.
HOST_DATASET_PATH="/Users/xuexiaoying/Desktop/Work/26-SickKids/icu_liver_cohort_v4"

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
    echo "         docker build -t $IMAGE ." >&2
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
    -v "$HOST_DATASET_PATH:$CONTAINER_DATASET:ro" \
    -v "$(pwd):/sdk" \
    -e "ATRIUMDB_DATASET_LOCATION=$CONTAINER_DATASET" \
    "$IMAGE" \
    "${CMD[@]}"
