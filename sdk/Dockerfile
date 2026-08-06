FROM python:3.11-slim

WORKDIR /sdk

# gcc and libmariadb-dev are required by the mariadb Python C extension.
RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc \
        libmariadb-dev \
        libgomp1 \
        liblz4-1 \
        libzstd1 \
    && rm -rf /var/lib/apt/lists/*

# Copy project source into the image.
COPY . .

# Install the SDK in editable mode with all testing dependencies.
# Editable mode (-e) means changes to the source are picked up without rebuilding.
RUN pip install --no-cache-dir -e ".[testing]"

EXPOSE 8000

# Default command: serve the API the dashboard calls.
# The dataset directory must be mounted and ATRIUMDB_DATASET_LOCATION set to it.
# Override on the command line to run tests instead, e.g.
#   docker run --rm atriumdb-sdk python -m pytest tests/test_dashboard_api.py -v -s
CMD ["uvicorn", "tests.mock_api.app:app", "--host", "0.0.0.0", "--port", "8000"]