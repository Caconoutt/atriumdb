FROM python:3.11-slim

WORKDIR /sdk

# System libraries required by the mariadb Python C extension (pulled in by [testing]).
# gcc is needed to compile the extension; libmariadb-dev provides the MariaDB client headers.
RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc \
        libmariadb-dev \
    && rm -rf /var/lib/apt/lists/*

# Copy project source into the image.
COPY . .

# Install the SDK in editable mode with all testing dependencies.
# Editable mode (-e) means changes to the source are picked up without rebuilding.
RUN pip install --no-cache-dir -e ".[testing]"

# Default command: run the dashboard cohort tests.
# Override this on the docker run command line to run a different test file or get a shell.
CMD ["python", "-m", "pytest", "tests/test_dashboard_api.py", "-v", "-s"]