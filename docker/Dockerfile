# AtriumDB query sandbox image.
#
# Two stages:
#   1. tsc-builder - compiles libTSC.so from tsc-lib/ for the architecture the
#      image is being built on. The libTSC.so checked into sdk/bin is aarch64,
#      so it will not load on an x86_64 server; building it here avoids that.
#   2. runtime     - python + the atriumdb SDK, installed editable so the SDK
#      picks up the freshly built sdk/bin/libTSC.so.
#
# Build context is the repository root (see docker/docker-compose.yml).

# ---------------------------------------------------------------- stage 1 ---
FROM debian:bookworm-slim AS tsc-builder

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        cmake \
        libzstd-dev \
        liblz4-dev \
    && rm -rf /var/lib/apt/lists/*

COPY tsc-lib/ /src/tsc-lib/

# Only the Block target is needed; the test targets are configured but not built.
RUN cmake -S /src/tsc-lib -B /src/build -DCMAKE_BUILD_TYPE=Release \
    && cmake --build /src/build --target Block -j "$(nproc)" \
    && cp /src/build/src/Block/libTSC.so /libTSC.so

# ---------------------------------------------------------------- stage 2 ---
FROM python:3.11-slim-bookworm AS runtime

# libgomp1  - libTSC.so is compiled with -fopenmp
# libzstd1 / liblz4-1 - runtime halves of the compression deps
# sqlite3   - so you can also poke at index.db from the CLI, not just python
RUN apt-get update && apt-get install -y --no-install-recommends \
        libgomp1 \
        libzstd1 \
        liblz4-1 \
        sqlite3 \
    && rm -rf /var/lib/apt/lists/*

COPY sdk/ /opt/atriumdb/sdk/
COPY --from=tsc-builder /libTSC.so /opt/atriumdb/sdk/bin/libTSC.so

# Editable install keeps the package at /opt/atriumdb/sdk, which is how
# AtriumSDK resolves the shared library (<package parent>/bin/libTSC.so).
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -e "/opt/atriumdb/sdk[cli]"

# Queries and outputs are bind-mounted here by docker-compose.
RUN mkdir -p /workspace/queries /workspace/out && chmod 777 /workspace/out
WORKDIR /workspace

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

CMD ["/bin/bash"]
