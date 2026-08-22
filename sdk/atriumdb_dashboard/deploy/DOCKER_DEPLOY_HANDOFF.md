# Handoff — containerising AtriumDB for the dashboard deployment

Written at the end of the dashboard-side deployment work, to be read at the
start of the AtriumDB-side work. Place this file in the **atriumdb** repo.

The dashboard repo (`SickKids_Dashboard`) is finished and its own README has a
full Deployment section. This note carries over only what the AtriumDB side
needs to know, and is self-contained — assume the reader has not seen the
dashboard repo.

---

## 1. What is being built

One server, four containers, defined by `docker-compose.yml` in the dashboard
repo. This repo supplies exactly one of them.

```
  host :8080 ──► frontend      nginx — serves the built SPA, proxies /api
                    │
                 backend       FastAPI — the dashboard API
                    ├────────► postgres      dashboard's own store
                    └────────► atriumdb-api   ◄── THIS REPO
                                    │
                                    └── dataset bind-mounted read-only
                                        at /data/atriumdb
```

Compose puts all four on one private bridge network where service names are
DNS hostnames. Only `frontend` publishes a host port; `atriumdb-api` is
reachable only from `backend`, at `http://atriumdb-api:8000`.

The dashboard never touches the dataset. Every interaction is HTTP, and
`atriumdb-api` is the only process that opens `meta/index.db` or the `tsc/`
files. That seam is what makes this split work — keep it.

## 2. How compose builds and runs this repo

From the dashboard's `docker-compose.yml`:

```yaml
  atriumdb-api:
    build:
      context: ../atriumdb/sdk          # ← requires the two repos be siblings
      dockerfile: atriumdb_dashboard/docker/Dockerfile
    # The image already defaults to this; keep it only if you want it explicit.
    command: uvicorn atriumdb_dashboard.deploy.server:app --host 0.0.0.0 --port 8000
    environment:
      ATRIUMDB_DATASET_LOCATION: /data/atriumdb
    volumes:
      - ${ATRIUMDB_DATASET_PATH}:/data/atriumdb:ro
```

Notes on each line:

* **`context: ../atriumdb/sdk`** — builds from this repo's existing
  `sdk/atriumdb_dashboard/docker/Dockerfile`, unmodified. It installs the SDK with the `testing` extra,
  which already pulls in `fastapi` and `uvicorn`.
* **`command:`** overrides that Dockerfile's `CMD` (which runs pytest) to serve
  the API instead. `WORKDIR` is `/sdk` and uvicorn puts the working directory
  on `sys.path`, so `tests.mock_api` resolves.
* **`ATRIUMDB_DATASET_LOCATION`** — see task 1; nothing reads it yet.
* **`:ro`** — the dashboard is a read path end to end, so nothing in the stack
  should be able to modify the source data.

This image builds and is ~1.39 GB (mariadb C extension, pandas, pyarrow,
fastparquet). Verified with `docker compose build`, exit 0.

Two constraints worth remembering: the SDK raises `OSError` on macOS
(`atrium_sdk.py`, "AtriumSDK is not currently supported on macOS"), so Docker
is mandatory for local work and the Linux `.so` in `sdk/bin/` is what actually
runs. And `sdk/atriumdb_dashboard/docker/Dockerfile.dockerignore` excludes
`tests/test_datasets/` but **not** `tests/`, which is why `tests/mock_api/` is
present in the image at all — the served app composes it.

## 3. Task 1 — the blocker

`atriumdb_dashboard/api/dependencies.py` currently reads:

```python
def get_sdk_instance() -> AtriumSDK:
    return AtriumSDK()
```

`AtriumSDK()` with no arguments raises
`ValueError: dataset location must be specified for sqlite mode`
(`atriumdb/atrium_sdk.py`, in the sqlite branch of `__init__`). The SDK reads
`ATRIUMDB_DATASET_LOCATION` only in its CLI (`atriumdb/cli/atriumdb_cli.py`),
never in the constructor — so the environment variable compose sets is
currently ignored.

The container therefore **starts fine and fails on the first request.** Minimum
fix:

```python
import os
from atriumdb import AtriumSDK

def get_sdk_instance() -> AtriumSDK:
    return AtriumSDK(dataset_location=os.environ["ATRIUMDB_DATASET_LOCATION"])
```

While in here, consider a second issue: this is a FastAPI `Depends`, so it runs
**per request**, and each call re-loads the C library and re-opens the SQLite
handler. Caching the instance (module-level singleton or `functools.lru_cache`)
would avoid paying that on every cohort query. Worth confirming an `AtriumSDK`
is safe to share across requests before doing it.

## 4. Task 2 — confirm the route surface

The dashboard calls exactly four routes. They are written down in one place —
`backend/app/atriumdb/client.py` in the dashboard repo — and were aligned
**to match this repo's served routes** rather than the other way round.

| Dashboard call | Served by | Response the dashboard parses |
|---|---|---|
| `POST /cohorts` | `cohort_endpoints.post_cohorts` | `MrnCohortResponse` |
| `POST /cohorts/statistics` | `cohort_endpoints.post_cohort_statistics` | `AggregateStatisticsResponse` |
| `GET /measures/hours` | `measures_endpoints.get_measure_total_hours` | list of per-measure hour rows |
| `GET /measures/` | `measures_endpoints.search_measures` | `{measure_id: measure_info}` |

Three details behind that table, each of which was a real mismatch:

1. **Statistics is `/cohorts/statistics`, not `/cohort/statistics`.** The
   router is mounted at prefix `/cohorts` by `mount_dashboard()`, called from
   `atriumdb_dashboard/deploy/server.py`; the dashboard was calling the singular
   and would have 404'd.
2. **There is no by-identity measure lookup.** The dashboard needs
   `(tag, freq_nhz, unit_code) → measure_id`, and was calling a
   `GET /measures/id` that does not exist. It now goes through
   `search_measures` with `measure_tag` / `freq` / `freq_units=nHz` / `unit`,
   then filters the returned mapping for an **exact** match on all three
   fields — necessary because `search_measures` matches `tag` as a *substring*,
   so a query for `ABP` would otherwise resolve to `ABP_MEAN`. If a proper
   exact-lookup route is added here, that client-side filtering can go.
3. **`GET /measures/hours` returns a bare list**, while the dashboard contract
   doc specifies `{"measures": [...]}`. The dashboard now accepts either. If
   this repo standardises on the wrapped shape, the dashboard needs no change.

Also note the dashboard sends a JSON **body on that GET** (the set of measures
it knows about, so the server could scope its answer). The mock declares no
parameters and ignores it. It survives today because the backend→atriumdb hop
does not cross nginx, but a body on a GET is fragile — most proxies strip it.
Either give the route a real request model or drop the body from the client.

**None of the four have been exercised against a live server.** The dashboard's
195 tests all mock the HTTP client, so they prove the code paths, not the wire
contract. A real round trip is the first thing to check once this container
serves.

## 5. What "done" looks like on this side

1. `docker compose up -d atriumdb-api` starts and stays up.
2. A request reaches the SDK and returns data rather than the
   `dataset location must be specified` ValueError.
3. All four routes above answer with the shapes the dashboard parses.
4. The dashboard's cohort flow works end to end through the browser.

Useful checks from the dashboard repo directory (the container has python but
no curl):

```bash
docker compose up -d atriumdb-api
docker compose logs -f atriumdb-api

# from inside the network, as the backend would call it
docker compose exec backend python -c \
  "import urllib.request; print(urllib.request.urlopen('http://atriumdb-api:8000/measures/hours').read()[:400])"

# confirm the dataset actually landed read-only in the container
docker compose exec atriumdb-api ls -l /data/atriumdb
```

## 6. Open question to settle

`tests/mock_api/` is a **test fixture** — no auth, no error envelope, no
configuration — and this plan deploys it as the production API layer. That may
be entirely fine for a closed research server, but it is a deliberate choice
someone should make rather than inherit. If a real AtriumDB server exists
elsewhere, the better move is to point the dashboard's `ATRIUMDB_URL` at it and
drop the `atriumdb-api` service from compose entirely; the dashboard needs no
code change for that, since the address is configuration.

## 7. Dashboard-side state, for reference

Complete and verified: Dockerfiles for backend and frontend, nginx config,
`docker-compose.yml`, `.env.docker.example`, the four route fixes above, and a
Deployment section in the dashboard README with a step-by-step server
procedure. `ruff` clean, 195 tests passing, all three images build.

The dataset lives in neither repo — it is copied to the server separately and
its absolute path goes in `ATRIUMDB_DATASET_PATH` in the dashboard's `.env`.
The directory must contain `meta/index.db` and `tsc/`.

One caveat if the dashboard repo is being cloned fresh: at the time of writing,
its `deploy` branch had not been pushed to origin.
