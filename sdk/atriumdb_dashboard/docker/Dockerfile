FROM python:3.11-slim

WORKDIR /sdk

# gcc and libmariadb-dev build the mariadb Python C extension.
# libgomp1, liblz4-1 and libzstd1 are loaded at runtime by the SDK's own
# libTSC.so; without them the import succeeds and the first query fails.
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

# Install the dashboard as its own distribution. It declares atriumdb as a
# dependency, which the editable install above already satisfies, so nothing is
# pulled from PyPI. A separate install is what lets the dashboard be versioned,
# built, and shipped independently of the SDK.
RUN pip install --no-cache-dir -e "./atriumdb_dashboard[testing]"

EXPOSE 8000

# Default command: serve the API the dashboard calls.
# The dataset directory must be mounted and ATRIUMDB_DATASET_LOCATION set to it.
#
# atriumdb_dashboard.deploy.server composes the upstream tests/mock_api app with the
# dashboard routers, so that file stays identical to upstream. WORKDIR is /sdk and
# uvicorn puts the working directory on sys.path, so both packages resolve.
#
# Override on the command line to run tests instead, e.g.
#   docker run --rm atriumdb-sdk python -m pytest tests/atriumdb_dashboard/ -v -s
CMD ["uvicorn", "atriumdb_dashboard.deploy.server:app", "--host", "0.0.0.0", "--port", "8000"]
