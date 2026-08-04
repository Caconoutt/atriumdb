# Deploying the AtriumDB API container

How to stand up the `atriumdb-api` service on a remote server you reach over SSH,
and how to operate it afterwards.

This repo supplies **one** of the four containers in the dashboard stack. The
stack is defined by `docker-compose.yml` in the **dashboard** repo
(`SickKids_Dashboard`), and every command that starts, stops, or rebuilds a
container is run from *that* directory — including the ones that act on this
service. Nothing here is deployed on its own.

```
  host :8080 ──► frontend      nginx — serves the SPA, proxies /api
                    │
                 backend       FastAPI — the dashboard API
                    ├────────► postgres      dashboard's own store
                    └────────► atriumdb-api   ◄── THIS REPO
                                    │
                                    └── dataset bind-mounted read-only
                                        at /data/atriumdb
```

Only `frontend` publishes a host port. `atriumdb-api` is reachable only from
`backend`, at `http://atriumdb-api:8000`, and it is the only process in the stack
that opens `meta/index.db` or the `tsc/` files.

---

## 1. What this repo contributes

Compose builds `sdk/Dockerfile` from the sibling checkout:

```yaml
  atriumdb-api:
    build:
      context: ../atriumdb/sdk         # ← the two repos must be siblings
    environment:
      ATRIUMDB_DATASET_LOCATION: /data/atriumdb
    volumes:
      - ${ATRIUMDB_DATASET_PATH}:/data/atriumdb:ro
```

The image's default command now serves the API
(`uvicorn tests.mock_api.app:app --host 0.0.0.0 --port 8000`), so the `command:`
override that older copies of `docker-compose.yml` carry is harmless but no
longer required. `WORKDIR` is `/sdk`, and uvicorn puts the working directory on
`sys.path`, so `tests.mock_api` resolves.

Two constants worth keeping in mind:

* The SDK **cannot run on macOS** — `AtriumSDK.__init__` raises
  `OSError: AtriumSDK is not currently supported on macOS`. Docker is mandatory
  even for local work; the Linux `.so` in `sdk/bin/` is what actually executes.
* `sdk/.dockerignore` excludes `tests/test_datasets/` but **not** `tests/`, which
  is why `tests/mock_api/` ships in the image at all. Do not add `tests/` to it.

## 2. Server prerequisites

* Docker Engine with the Compose plugin (`docker compose version` must work).
  Compose v1 (`docker-compose`, hyphenated) is not supported by these files.
* `git`.
* Enough disk for the image (~1.4 GB: mariadb C extension, pandas, pyarrow,
  fastparquet) plus the Postgres volume and the dataset itself.
* The user running compose must be in the `docker` group, or every command needs
  `sudo`. Pick one and stay consistent — images built under `sudo` are not
  visible to a rootless daemon and vice versa.

## 3. Deployment order

The order matters in exactly two places: the dataset must be on disk before the
first `up` (compose bind-mounts it, and a missing host path silently becomes an
empty directory), and both repos must be checked out before the first `build`
(compose reaches into `../atriumdb/sdk`). Everything else is linear.

### Step 0 — prepare the dataset locally, before copying it

If the dataset has waveform data but no ADT/encounter records, it needs MRNs,
beds and encounter rows seeded before the dashboard can query it. Run this on
your **own machine**, against your local copy, before the transfer:

```bat
REM Windows
cd atriumdb\sdk
python scripts\prepare_dataset.py "C:\path\to\local\dataset"
```

```bash
# macOS / Linux
cd atriumdb/sdk
python3 scripts/prepare_dataset.py /path/to/local/dataset
```

Requirements: Python 3.7+ and nothing else. The script uses only the stdlib
`sqlite3` module and never imports the SDK, so it runs on any OS — no Docker, no
virtualenv, no install, and the macOS `OSError` does not apply.

It writes to `meta/index.db` in place, taking a timestamped `.bak-<date>` copy
first (`--no-backup` skips that). It is safe to re-run: encounters are deleted
and re-derived every time, and MRNs are a deterministic hash of `patient_id`, so
repeated runs produce identical rows. `-h` lists the options, and the module
docstring documents each step it performs.

Every run ends with a row count per table. Add `--dump` to print the rows
themselves — `--limit N` caps rows per table (default 20, `0` for all) and
`--tables patient,encounter` narrows which ones. **`--dump` prints patient data
to the console**, so redirect it to a file you control rather than scrolling it,
and keep `--limit` on for `block_index` and `interval_index`, which can hold
millions of rows:

```bat
python scripts\prepare_dataset.py "C:\path\to\dataset" --dump --tables patient,encounter > dump.txt
```

Two Windows-specific notes, both already handled by the script but worth knowing:
output is pure ASCII and stdout is forced to UTF-8, so redirecting to a file
cannot fail with `UnicodeEncodeError` on a cp1252 console; and a trailing
backslash is stripped from the path. Do not leave that backslash *inside* the
quotes when you type it — `cmd.exe` reads `\"` as an escaped quote, so
`"C:\path\to\dataset\"` breaks argument parsing before Python ever sees it.
If the database is open in DB Browser for SQLite, close it first — Windows takes
a mandatory lock and the backup copy will fail.

Doing this locally is what lets the server side stay unchanged — the dataset
arrives already prepared, so `atriumdb-api` keeps its read-only mount and no
container ever needs write access to patient data. If you must instead run it on
the server, the script ships in the image, but you have to mount the dataset
writable to do it:

```bash
docker run --rm -v "$ATRIUMDB_DATASET_PATH:/data/atriumdb" \
  sickkids_dashboard-atriumdb-api python scripts/prepare_dataset.py /data/atriumdb
```

Stop the stack first if it is already up — `atriumdb-api` holds an open SQLite
handle on the same file.

### Step 1 — put the dataset on the server

Copy it by whatever route your data-transfer policy allows (`rsync -a` over SSH
is typical). It must end up as a directory containing:

```
<dataset-dir>/
├── meta/index.db
└── tsc/
```

Note its **absolute path** — it goes in the dashboard's `.env` as
`ATRIUMDB_DATASET_PATH`. Nothing in either repo stores it.

The mount is read-only, so the dataset does not need to be writable by the
Docker user; it does need to be *readable* by it.

### Step 2 — clone both repos as siblings

```bash
ssh <user>@<server>
mkdir -p ~/sickkids && cd ~/sickkids

git clone <atriumdb-remote> atriumdb
git clone <dashboard-remote> SickKids_Dashboard
```

The directory names matter — `context: ../atriumdb/sdk` is resolved relative to
`SickKids_Dashboard/docker-compose.yml`. The final layout must be:

```
~/sickkids/
├── SickKids_Dashboard/     ← run every compose command from here
└── atriumdb/               ← this repo
```

Check out the branch carrying the deployment changes in **both** repos (at the
time of writing: `deploy` on this repo; confirm the dashboard's branch name and
that it has been pushed to origin — it had not been at handoff time):

```bash
cd ~/sickkids/atriumdb && git checkout deploy && git log --oneline -1
cd ~/sickkids/SickKids_Dashboard && git checkout <branch> && git log --oneline -1
```

### Step 3 — configure the dashboard's `.env`

All four services read their configuration from one file, in the dashboard repo:

```bash
cd ~/sickkids/SickKids_Dashboard
cp .env.docker.example .env
$EDITOR .env
```

The two values that decide whether *this* container works:

| Variable | What it must be |
|---|---|
| `ATRIUMDB_DATASET_PATH` | Absolute host path from step 1 |
| `ATRIUMDB_STATISTICS_TIMEOUT_SECONDS` | Backend's per-request budget for `POST /cohorts/statistics`; raise it if aggregation over large cohorts times out |

Also set `JWT_SECRET` to a generated value and change `POSTGRES_PASSWORD` —
neither affects this container, but the stack is not safe to expose without them.

### Step 4 — build

```bash
cd ~/sickkids/SickKids_Dashboard
docker compose build
```

First build takes several minutes (compiling the MariaDB extension dominates).
To build only this service:

```bash
docker compose build atriumdb-api
```

### Step 5 — start this service alone and prove it before the rest

```bash
docker compose up -d atriumdb-api
docker compose logs -f atriumdb-api      # expect: "Uvicorn running on http://0.0.0.0:8000"
```

Confirm the dataset actually landed, and read-only:

```bash
docker compose exec atriumdb-api ls -l /data/atriumdb
# must list meta/ and tsc/ — an empty listing means ATRIUMDB_DATASET_PATH is wrong
docker compose exec atriumdb-api touch /data/atriumdb/_wtest
# must fail with "Read-only file system"
```

Then run the route checks in §5. Do this **before** starting the other three
services — a dataset-path mistake surfaces here in seconds, versus as an opaque
502 through two proxies later.

### Step 6 — start the rest

```bash
docker compose up -d
docker compose ps        # all four Up; postgres and backend healthy
```

The dashboard is then on `http://<server>:${HTTP_PORT}` (default 8080).

### Step 7 — end-to-end check

Log into the dashboard in a browser and run one cohort query through to
statistics. That is the only check that exercises the full chain
(browser → nginx → backend → this container → SDK → dataset). Watch both logs
while you do it:

```bash
docker compose logs -f backend atriumdb-api
```

## 4. Operating the container

Every command runs from `~/sickkids/SickKids_Dashboard`.

| Task | Command |
|---|---|
| Status | `docker compose ps` |
| Follow logs | `docker compose logs -f atriumdb-api` |
| Last 200 log lines | `docker compose logs --tail=200 atriumdb-api` |
| Restart (no code change) | `docker compose restart atriumdb-api` |
| Stop just this service | `docker compose stop atriumdb-api` |
| Shell inside it | `docker compose exec atriumdb-api bash` |
| Python REPL with the SDK | `docker compose exec atriumdb-api python` |
| Resource usage | `docker stats` |
| Stop everything | `docker compose down` (add `-v` **only** to destroy the Postgres volume) |

### Deploying a code change from this repo

```bash
cd ~/sickkids/atriumdb && git pull
cd ~/sickkids/SickKids_Dashboard
docker compose build atriumdb-api
docker compose up -d atriumdb-api      # recreates only this container
```

The other three services keep running; the backend reconnects on its next
request. A `docker compose restart` alone is **not** enough — the image has to be
rebuilt, because `COPY . .` bakes the source in at build time. (The `-e` editable
install means the source tree inside the image is live, but the image's copy of
it is only refreshed by a rebuild.)

### Rolling back

```bash
cd ~/sickkids/atriumdb && git checkout <last-good-sha>
cd ~/sickkids/SickKids_Dashboard && docker compose build atriumdb-api && docker compose up -d atriumdb-api
```

### Reclaiming disk after several rebuilds

```bash
docker image prune -f            # dangling images only — safe
docker system df                 # see what is actually using space
```

Never `docker system prune -a --volumes` on this box: `--volumes` destroys the
dashboard's Postgres data.

## 5. Verifying the four routes

The dashboard calls exactly four routes on this service. The image has Python but
**no curl**, so probe with `urllib`. Run these from the dashboard directory; the
first form talks to the container directly, which is enough for all four.

```bash
# liveness
docker compose exec atriumdb-api python -c \
  "import urllib.request;print(urllib.request.urlopen('http://localhost:8000/health').read())"

# 1. per-measure recorded hours  → a JSON list
docker compose exec atriumdb-api python -c \
  "import urllib.request;print(urllib.request.urlopen('http://localhost:8000/measures/hours').read()[:400])"

# 2. measure search → {measure_id: {...}} (note the trailing slash)
docker compose exec atriumdb-api python -c \
  "import urllib.request;print(urllib.request.urlopen('http://localhost:8000/measures/').read()[:400])"
```

Once `backend` is up, check the hop the dashboard actually uses — service name,
not localhost:

```bash
docker compose exec backend python -c \
  "import urllib.request;print(urllib.request.urlopen('http://atriumdb-api:8000/measures/hours').read()[:400])"
```

The two POST routes need a body, so drive them from a heredoc:

```bash
docker compose exec -T atriumdb-api python - <<'PY'
import json, urllib.request
body = {...}   # a CohortDefinitionRequest — see atriumdb/dashboard/schemas.py
req = urllib.request.Request(
    "http://localhost:8000/cohorts",
    data=json.dumps(body).encode(),
    headers={"Content-Type": "application/json", "X-Request-ID": "manual-check"},
)
print(urllib.request.urlopen(req).read()[:600])
PY
```

`POST /cohorts/statistics` is the same shape with an `AggregateStatisticsRequest`
body. It **requires a non-empty `X-Request-ID`** and returns 400 without one —
that is deliberate, not a fault.

| Dashboard call | Served by | Response it parses |
|---|---|---|
| `POST /cohorts` | `cohort_endpoints.post_cohorts` | `MrnCohortResponse` |
| `POST /cohorts/statistics` | `cohort_endpoints.post_cohort_statistics` | `AggregateStatisticsResponse` |
| `GET /measures/hours` | `measures_endpoints.get_measure_total_hours` | bare list of per-measure hour rows |
| `GET /measures/` | `measures_endpoints.search_measures` | `{measure_id: measure_info}` |

Two behaviours the dashboard depends on, so do not "fix" them casually:

* `/cohorts/statistics` is **plural** `/cohorts`, matching the router prefix.
* There is no by-identity measure lookup. The dashboard calls `GET /measures/`
  with `measure_tag` / `freq` / `freq_units=nHz` / `unit` and then filters for an
  *exact* match on all three, because `search_measures` matches the tag as a
  substring — a query for `ABP` otherwise resolves to `ABP_MEAN`. If an exact
  lookup route is ever added here, that client-side filter can go.

`GET /measures/hours` returns a bare list while the dashboard's contract doc
specifies `{"measures": [...]}`; the dashboard accepts either, so no change is
needed unless you want to standardise. Note also that the dashboard sends a JSON
body on that GET. The route declares no parameters and ignores it. It survives
because the backend→atriumdb hop does not cross nginx — but a body on a GET is
fragile, and if a proxy is ever put between them, drop the body or give the route
a real request model.

## 6. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `ATRIUMDB_DATASET_LOCATION is not set` at first request | env var missing from the service | It is set in `docker-compose.yml`; confirm you are on the deploy branch of the dashboard repo |
| `No Dataset found at location /data/atriumdb` | mount points at the wrong directory, or one level too high/low | `docker compose exec atriumdb-api ls -l /data/atriumdb` — must show `meta/` and `tsc/` |
| `/data/atriumdb` is empty | `ATRIUMDB_DATASET_PATH` does not exist on the host; Docker created an empty dir | Fix the path in `.env`, then `docker compose up -d atriumdb-api` |
| `Permission denied` reading `meta/index.db` | dataset not readable by the Docker user | `chmod`/`chown` on the host, or run compose as a user that can read it |
| `attempt to write a readonly database` | something tried to write through the read-only mount | A read path should never do this — capture the traceback; do **not** drop `:ro` to make it go away |
| `mrn column is using an INTEGER type` | dataset predates the TEXT-MRN schema | Schema upgrade writes, so it cannot happen against a `:ro` mount — upgrade a writable copy separately, then redeploy |
| `OSError: AtriumSDK is not currently supported on macOS` | ran the SDK outside the container | Always go through Docker |
| Backend logs `AtriumDB unreachable at http://atriumdb-api:8000` | this container is down or crashed at import | `docker compose ps`, then `docker compose logs atriumdb-api` |
| First request after start is slow, later ones fast | expected — the SDK is built once and cached | none |
| Other requests hang while a long statistics query runs | the routes are `async def` around synchronous SDK calls, so each request holds the event loop until it finishes — requests are effectively serialized | expected under one user; if concurrent use becomes real, either declare the routes `def` (FastAPI then runs them in its threadpool) or add uvicorn workers |

## 7. Optional dashboard-side follow-up

Now that `GET /health` exists, the dashboard's compose can wait for this service
properly instead of merely for the container to exist. In
`SickKids_Dashboard/docker-compose.yml`:

```yaml
  atriumdb-api:
    healthcheck:
      test: ["CMD", "python", "-c",
             "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')"]
      interval: 15s
      timeout: 5s
      retries: 5
      start_period: 20s

  backend:
    depends_on:
      atriumdb-api:
        condition: service_healthy      # was: service_started
```

That change belongs to the dashboard repo, not this one.

## 8. Standing caveat

`tests/mock_api/` is a **test fixture** — no authentication, no error envelope,
no configuration surface — and this deployment serves it as the production API
layer. That is defensible on a closed research server where the only client is
the backend on a private bridge network, but it is a choice, not an accident. If
a real AtriumDB server exists elsewhere, the better move is to point the
dashboard's `ATRIUMDB_URL` at it and delete the `atriumdb-api` service from
compose entirely — the dashboard needs no code change, since the address is
configuration.

If this path is kept long term, move `tests/mock_api/` to a real package (e.g.
`atriumdb/server/`) so production stops depending on `tests/` remaining in the
image.
