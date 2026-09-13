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

## 0. The deployment, start to finish

Every command runs from `~/sickkids/SickKids_Dashboard` unless stated otherwise.
Each step links to the section that explains it; the detail matters most the
first time and on the steps that touch credentials.

### First, decide which backends this deployment uses

The container opens **two** SDK instances now, and they are configured
separately. Which ones you need decides which variables you set.

| | metadata SDK (`get_meta_sdk`) | data SDK (`get_data_sdk`) |
| --- | --- | --- |
| serves | `POST /cohorts`, `GET /measures/hours` | `POST /cohorts/statistics`, `POST /cohorts/timeseries` |
| reads | a SQLite dataset, or MariaDB | the AtriumDB API over Auth0 |
| configure with | `ATRIUMDB_METADATA_CONNECTION_TYPE` + either `ATRIUMDB_DATASET_LOCATION` or the `ATRIUMDB_MARIA_*` set | `ATRIUMDB_API_URL` + the `AUTH0_*` set |

Two shapes in practice:

* **Local / self-contained** — SQLite dataset, no API. Leave `ATRIUMDB_API_URL`
  unset; statistics and time-series return **503** and the other two routes work
  normally. This is the pre-UAT deployment, unchanged.
* **UAT** — MariaDB for metadata, the AtriumDB API for waveforms. Both sets of
  variables required.

Mixing is allowed (SQLite metadata + a remote API, say), because each endpoint
uses exactly one SDK and neither knows about the other.

### The steps

| # | Step | Where | Section |
| --- | --- | --- | --- |
| 1 | Prepare the dataset — seed MRNs, beds, encounters | your own machine | [Step 0](#step-0--prepare-the-dataset-locally-before-copying-it) |
| 2 | Copy the dataset to the server | your machine → server | [Step 1](#step-1--put-the-dataset-on-the-server) |
| 3 | Clone `atriumdb` and `SickKids_Dashboard` as siblings | server | [Step 2](#step-2--clone-both-repos-as-siblings) |
| 4 | **Obtain credentials** — read-only MariaDB account, Auth0 M2M client, API URL | — | [Step 2.5](#step-25--obtain-and-place-credentials-uat-only) |
| 5 | **Place credentials** so the container can read them | server | [Step 2.5](#step-25--obtain-and-place-credentials-uat-only) |
| 6 | Configure the dashboard's `.env` | server | [Step 3](#step-3--configure-the-dashboards-env) |
| 7 | `docker compose config` — check substitution before building | server | [Step 4](#step-4--build) |
| 8 | `docker compose build` | server | [Step 4](#step-4--build) |
| 9 | `docker compose up -d atriumdb-api` — this service alone | server | [Step 5](#step-5--start-this-service-alone-and-prove-it-before-the-rest) |
| 10 | Check the startup log names the right backends | server | [Step 5](#step-5--start-this-service-alone-and-prove-it-before-the-rest) |
| 11 | `GET /health/ready` — both backends reachable | server | [Step 5](#step-5--start-this-service-alone-and-prove-it-before-the-rest) |
| 12 | Probe the five routes | server | [§5](#5-verifying-the-routes) |
| 13 | `docker compose up -d` — start the other three | server | [Step 6](#step-6--start-the-rest) |
| 14 | One cohort query end to end, in a browser | browser | [Step 7](#step-7--end-to-end-check) |

Condensed, once the credentials are in place:

```bash
cd ~/sickkids/SickKids_Dashboard
docker compose config                       # 7  substitution sane?
docker compose build                        # 8  ~minutes on a cold cache
docker compose up -d atriumdb-api           # 9  this service alone
docker compose logs -f atriumdb-api         # 10 Ctrl-C to detach
docker compose exec atriumdb-api python -c \
  "import urllib.request;print(urllib.request.urlopen('http://localhost:8000/health/ready').read())"
                                            # 11 {"metadata":"ok","data_api":"ok"}
docker compose up -d                        # 13 the other three
docker compose ps                           # all four Up
```

Steps 9–12 exist to fail fast. A wrong dataset path, an unreachable MariaDB or a
rejected Auth0 client shows up there in seconds, rather than as an opaque 502
through two proxies once everything is running.

### What "done" looks like

* `docker compose ps` — all four `Up`, `postgres` and `backend` `(healthy)`.
* `GET /health/ready` — `{"metadata": "ok", "data_api": "ok"}`, or
  `{"metadata": "ok", "data_api": "not configured"}` for a local deployment.
* The startup log names the backends it opened, with no password in sight:
  `Dashboard configuration: metadata: mariadb ro@db.host:3306/atrium | data api: https://host/api/v1 (auth0 client_credentials)`
* One cohort query in the browser returns statistics.

---

## 1. What this repo contributes

Compose builds `sdk/atriumdb_dashboard/docker/Dockerfile` from the sibling checkout:

```yaml
  atriumdb-api:
    build:
      context: ../atriumdb/sdk         # ← the two repos must be siblings
      dockerfile: atriumdb_dashboard/docker/Dockerfile
    environment:
      ATRIUMDB_DATASET_LOCATION: /data/atriumdb
    env_file:
      - ./atriumdb-api.env             # ← UAT only; see step 2.5
    volumes:
      - ${ATRIUMDB_DATASET_PATH}:/data/atriumdb:ro
```

The `env_file:` line is the UAT addition — it carries the MariaDB and Auth0
settings. Omit it for a local SQLite deployment, where
`ATRIUMDB_DATASET_LOCATION` alone is enough.

### Configuration environment variables

`config.py` is the only module that reads the environment, and
`deploy/server.py` validates the whole set **at import**. A missing or malformed
value therefore stops the container at startup with a message naming the
variable, rather than surfacing as a 500 on the first real request.

| Variable | Required when | Effect |
|---|---|---|
| `ATRIUMDB_METADATA_CONNECTION_TYPE` | always (defaults to `sqlite`) | `sqlite` or `mariadb`. The default preserves the pre-UAT deployment exactly. |
| `ATRIUMDB_DATASET_LOCATION` | always | The dataset directory in SQLite mode. **Still required in `mariadb` mode**, where it is passed as `tsc_file_location` purely to satisfy the SDK constructor — it builds a file handler before it looks at the connection type. Nothing reads a `.tsc` file through the metadata SDK, so any existing directory will do. |
| `ATRIUMDB_MARIA_HOST` / `_PORT` / `_USER` / `_PASSWORD` / `_DATABASE` | `mariadb` mode | The metadata database. Ask for a **read-only** account — see step 2.5. `_PORT` defaults to 3306. |
| `ATRIUMDB_API_URL` | statistics / time-series | Base URL of the AtriumDB API, **including any version segment**. The SDK appends bare endpoint names, so a versioned API must carry the prefix here or every request 404s. Leave unset to disable those two routes with a 503. |
| `AUTH0_TENANT` / `_CLIENT_ID` / `_CLIENT_SECRET` / `_AUDIENCE` | when `ATRIUMDB_API_URL` is set | Machine-to-machine credentials. `AUTH0_AUDIENCE` is an Auth0 identifier and is **not** the same value as `ATRIUMDB_API_URL`, though they often look similar — a mismatch is the usual cause of a 401. |
| `AUTH0_GRANT_TYPE` | optional | Defaults to `client_credentials`, the only flow supported. |
| `ATRIUMDB_API_TOKEN` | optional | A pre-minted token, which skips the Auth0 round trip. For iterating, not for deployment. |

> **Setting `ATRIUMDB_API_URL` without the `AUTH0_*` set is refused at startup**,
> naming the ones that are missing. Half-configured is treated as a mistake
> rather than a degraded mode — the alternative is a container that starts
> cleanly and 503s every statistics request.

> **The `dockerfile:` key is required.** The Dockerfile lives inside the
> dashboard package rather than at the build-context root, so a compose file
> carrying only `context:` will fail with "Dockerfile not found". Context stays
> `../atriumdb/sdk` — the build needs the whole `sdk/` tree.

### Logging environment variables

Both are optional; `server.py` configures logging at import either way.

| Variable | Default | Effect |
|---|---|---|
| `ATRIUMDB_DASHBOARD_LOG_LEVEL` | `INFO` | Level for every `atriumdb_dashboard.*` logger. Set `DEBUG` to surface per-request diagnostics — measure and value-range resolution, per-cohort counts, and the full sample grid fetched for each patient. An unrecognised value logs a warning and falls back rather than stopping the container. |
| `ATRIUMDB_DASHBOARD_EXCLUSION_LOG` | unset | Path for the exclusion audit trail. When set, the statistics and time-series `*.exclusions` loggers write there instead of the console, giving a file that records why each patient was dropped. Mount it on a writable volume — an unwritable path fails at startup by design, since an audit trail that silently goes nowhere is worse than a container that refuses to boot. |

> **`DEBUG` is not a level to leave on.** The resolvers log the whole NaN-filled
> value grid per patient at DEBUG; a 24 h window at 1 Hz is roughly 1 MB per
> patient per request, so a few hundred patients is hundreds of MB of log for a
> single call. Turn it on for a targeted trace, then turn it back off.

Without these, uvicorn's own `dictConfig` configures only its three loggers, so
the dashboard's records fall through to Python's `lastResort` handler — every
`debug()` call discarded, and the surviving warnings printed with no timestamp,
level or logger name.

The image's default command serves the API
(`uvicorn atriumdb_dashboard.deploy.server:app --host 0.0.0.0 --port 8000`), so the
`command:` override that older copies of `docker-compose.yml` carry should be
removed or updated — an override still naming `tests.mock_api.app:app` would
start a server with **no dashboard routes**. `WORKDIR` is `/sdk`, and uvicorn puts
the working directory on `sys.path`, so both `atriumdb_dashboard` and
`tests.mock_api` resolve.

`atriumdb_dashboard.deploy.server` composes the upstream `tests/mock_api`
application with the dashboard routers and adds `/health`. Serving that module
rather than editing `tests/mock_api/app.py` is what keeps every file under
`sdk/atriumdb/` and `sdk/tests/mock_api/` byte-identical to upstream, so SDK
updates merge without touching dashboard work.

Two constants worth keeping in mind:

* The SDK **cannot run on macOS** — `AtriumSDK.__init__` raises
  `OSError: AtriumSDK is not currently supported on macOS`. Docker is mandatory
  even for local work; the Linux `.so` in `sdk/bin/` is what actually executes.
* `sdk/atriumdb_dashboard/docker/Dockerfile.dockerignore` excludes
  `tests/test_datasets/` but **not** `tests/`, which is why `tests/mock_api/`
  ships in the image at all. Do not add `tests/` to it.

## 2. Server prerequisites

* Docker Engine with the Compose plugin (`docker compose version` must work).
  Compose v1 (`docker-compose`, hyphenated) is not supported by these files.
* `git`.
* Enough disk for the image (~1.4 GB: mariadb C extension, pandas, pyarrow,
  fastparquet) plus the Postgres volume and the dataset itself.
* The user running compose must be in the `docker` group, or every command needs
  `sudo`. Pick one and stay consistent — images built under `sudo` are not
  visible to a rootless daemon and vice versa.
* **For UAT only**, outbound network from the container to:
  * the Auth0 tenant over `https://` — token minting;
  * the AtriumDB API over **both `https://` and `wss://`** — metadata goes over
    REST, but block transfers use the websocket, so a firewall that permits only
    HTTPS gives a container that authenticates and reads measures happily and
    then hangs on the first statistics request;
  * the MariaDB host on its port (3306 by default).

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
python3 atriumdb_dashboard/deploy/prepare_dataset.py /path/to/local/dataset
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
  sickkids_dashboard-atriumdb-api python atriumdb_dashboard/deploy/prepare_dataset.py /data/atriumdb
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

### Step 2.5 — obtain and place credentials (UAT only)

Skip this entirely for a local SQLite deployment.

**What to ask for.**

| From | What | Notes |
|---|---|---|
| whoever owns the MariaDB | host, port, database, username, password | Ask explicitly for a **read-only** account. Nothing the dashboard does writes, and read-only turns a mistake — an accidental `auto_upgrade=True`, say — into a permissions error instead of a schema migration on a shared server. |
| whoever owns the API | the Auth0 client id, client secret, audience and tenant, plus the **API base URL** | The base URL is not derivable from the token: an Auth0 token carries `iss` and `aud`, neither of which has to be the API's address. Ask for it rather than guessing. |

The MariaDB account needs `SELECT` on: `settings`, `measure`, `block_index`,
`encounter`, `bed`, `unit`, `patient`, `patient_history`. `settings` is easy to
miss — the SDK constructor reads it before serving anything.

**Where to put them.** They belong in a file on the server that compose reads and
injects, *outside* the build context so no image layer can contain them:

```bash
cd ~/sickkids/SickKids_Dashboard
cp ../atriumdb/sdk/atriumdb_dashboard/.env.example ./atriumdb-api.env
$EDITOR ./atriumdb-api.env
chmod 600 ./atriumdb-api.env
```

Then reference it from the `atriumdb-api` service, as shown in §1:

```yaml
    env_file:
      - ./atriumdb-api.env
```

Fill in, at minimum:

```
ATRIUMDB_METADATA_CONNECTION_TYPE=mariadb
ATRIUMDB_MARIA_HOST=...
ATRIUMDB_MARIA_USER=...
ATRIUMDB_MARIA_PASSWORD=...
ATRIUMDB_MARIA_DATABASE=...

ATRIUMDB_API_URL=https://host/api/v1
AUTH0_TENANT=your-tenant.us.auth0.com
AUTH0_CLIENT_ID=...
AUTH0_CLIENT_SECRET=...
AUTH0_AUDIENCE=...
```

`ATRIUMDB_DATASET_LOCATION` stays set in `docker-compose.yml` — in `mariadb` mode
it only satisfies the SDK constructor, as §1 explains.

Three rules, none of them optional:

* **Never `-e` on the command line.** `docker run -e PASSWORD=…` puts the value
  in the host's process list and in shell history. `env_file` keeps it in a file
  you control the permissions of.
* **Never inside the build context.** `sdk/` is the build context and the
  Dockerfile ends in `COPY . .`. A `.env` there would be baked into an image
  layer, and deleting it later does not remove it from the layer. The
  `.dockerignore` excludes `**/.env` precisely so a slip is harmless, but keeping
  the real file outside the context is the actual protection.
* **If a credential was ever inside an image, rotate it.** Excluding a file from
  future builds does nothing about images already built.

> A `.env` inside the bind-mounted `sdk/` tree also works — `config.py` looks for
> one beside the package and in the working directory — and that is the right
> shape for local development. For a server, `env_file` is better: the secret
> lives in one file with its own permissions, rather than inside a source tree
> that gets pulled and rebuilt.

### Step 3 — configure the dashboard's `.env`

All four services read their configuration from one file, in the dashboard repo:

```bash
cd ~/sickkids/SickKids_Dashboard
cp .env.docker.example .env
$EDITOR .env
```

This is the **dashboard's** configuration. This container's own MariaDB and
Auth0 settings live in `atriumdb-api.env` from step 2.5, not here — keeping them
apart means the dashboard's `.env`, which several services read, never holds the
AtriumDB credentials.

The two values that decide whether *this* container works:

| Variable | What it must be |
|---|---|
| `ATRIUMDB_DATASET_PATH` | Absolute host path from step 1 |
| `ATRIUMDB_STATISTICS_TIMEOUT_SECONDS` | Backend's per-request budget for `POST /cohorts/statistics`; raise it if aggregation over large cohorts times out. Over UAT this budget matters more than it did locally — every window now crosses the network |

Also set `JWT_SECRET` to a generated value and change `POSTGRES_PASSWORD` —
neither affects this container, but the stack is not safe to expose without them.

### Steps 4–7 at a glance

Run these in this order, from `~/sickkids/SickKids_Dashboard`, waiting for each
to finish before starting the next:

```bash
cd ~/sickkids/SickKids_Dashboard   # 1. every compose command runs from here
docker compose config              # 2. check .env substitution before building
docker compose build               # 3. build the three buildable images
docker compose up -d atriumdb-api  # 4. start THIS service alone
docker compose logs -f atriumdb-api        # 5. watch it boot, Ctrl-C to detach
docker compose exec atriumdb-api ls -l /data/atriumdb   # 6. prove the mount
docker compose exec atriumdb-api python -c \
  "import urllib.request;print(urllib.request.urlopen('http://localhost:8000/health/ready').read())"
                                   # 7. prove both backends
docker compose up -d               # 8. start the other three
docker compose ps                  # 9. all four Up
docker compose logs -f backend atriumdb-api   # 10. watch while you click through
```

Steps 4–7 exist to fail fast: a wrong dataset path, an unreachable MariaDB or a
rejected Auth0 client shows up there in seconds, rather than as an opaque 502
through two proxies after everything is running. The rest of this section
explains each command.

### Step 4 — build

```bash
cd ~/sickkids/SickKids_Dashboard
docker compose build
```

Line by line:

* `cd ~/sickkids/SickKids_Dashboard` — not optional and not just convenience.
  Compose reads `docker-compose.yml` and `.env` from the *current directory*,
  and resolves `context: ../atriumdb/sdk` relative to the compose file. Run it
  from anywhere else and you get either "no configuration file provided" or a
  build context pointing at the wrong tree. Every command from here to the end
  of the document assumes this directory.
* `docker compose build` — builds an image for every service that has a
  `build:` section: `frontend`, `backend`, and `atriumdb-api`. `postgres` is a
  pulled image, so there is nothing to build for it. This **starts nothing** —
  no container runs, no port opens, the dataset is not touched. It is safe to
  run repeatedly.

First build takes several minutes (compiling the MariaDB extension dominates).
Later builds reuse the layer cache and are much faster, unless you changed
something early in the Dockerfile.

Two optional commands around it:

```bash
docker compose config                    # print the fully-resolved compose file
docker compose build atriumdb-api        # build only this service
```

* `docker compose config` — renders **the dashboard's `docker-compose.yml`**,
  all four services including `atriumdb-api`, with every `${VAR}` from `.env`
  already substituted, then exits. It is a pure text operation on that one file:
  it does **not** read anything inside the atriumdb repo, does not open
  `sdk/atriumdb_dashboard/docker/Dockerfile`, and does not check that `../atriumdb/sdk` exists — it only
  rewrites that path to an absolute one. A context pointing at a repo you never
  cloned renders here without complaint and only fails at `build`.

  What it does catch is an **unset or empty** variable. A blank
  `ATRIUMDB_DATASET_PATH` makes the volume spec collapse to `:/data/atriumdb:ro`
  and `config` hard-errors with `invalid spec: empty section between colons` —
  which is exactly the mistake worth catching before a multi-minute build. It
  does **not** catch a path that is non-empty but wrong: a typo'd path renders
  cleanly, and only the `ls -l` check in step 5 exposes it.
* `docker compose build atriumdb-api` — same as above but limited to this one
  service. Use it when only this repo changed; it skips the frontend and backend
  images entirely.

### Step 5 — start this service alone and prove it before the rest

```bash
docker compose up -d atriumdb-api
docker compose logs -f atriumdb-api      # expect: "Uvicorn running on http://0.0.0.0:8000"
```

* `docker compose up -d atriumdb-api` — creates the stack's bridge network if it
  does not exist, creates the `atriumdb-api` container from the image built in
  step 4, applies the environment and the read-only bind mount, and starts it.
  Naming the service limits `up` to that service plus anything it `depends_on`
  — this one depends on nothing, so exactly one container starts. `-d` is
  detached: it returns to the shell instead of streaming logs. Without `-d` the
  container dies when you close the SSH session.
* `docker compose logs -f atriumdb-api` — prints this container's stdout/stderr
  and `-f` keeps following as new lines arrive. **Ctrl-C stops the log stream,
  not the container** — the service keeps running after you detach.

  Two lines to look for, in order:

  ```
  Dashboard configuration: metadata: mariadb ro@db.host:3306/atrium | data api: https://host/api/v1 (auth0 client_credentials)
  Uvicorn running on http://0.0.0.0:8000
  ```

  The first is `check_configuration()` reporting what it resolved — read it, and
  confirm it names the backends you meant. Secrets are redacted, so a password
  appearing there is a bug worth reporting. For a local deployment it reads
  `data api: not configured`, and a warning follows saying the statistics and
  time-series routes will 503.

  A configuration problem stops the container **before** uvicorn starts, so a
  `ConfigError` naming a variable and no "Uvicorn running" line means exactly
  what it says. Anything else — a traceback, an immediate exit — means the image
  is wrong.

Confirm the dataset actually landed, and read-only:

```bash
docker compose exec atriumdb-api ls -l /data/atriumdb
# must list meta/ and tsc/ — an empty listing means ATRIUMDB_DATASET_PATH is wrong
docker compose exec atriumdb-api touch /data/atriumdb/_wtest
# must fail with "Read-only file system"
```

* `docker compose exec atriumdb-api <cmd>` — runs `<cmd>` inside the *already
  running* container. (`exec` needs the container up; `run` would start a
  throwaway second one, which is not what you want here.)
* `ls -l /data/atriumdb` — the container-side end of the bind mount. You must
  see `meta/` and `tsc/`. An empty listing means the host path in `.env` does not
  exist and Docker silently created an empty directory in its place — fix
  `ATRIUMDB_DATASET_PATH` and re-run `docker compose up -d atriumdb-api`. Seeing
  the dataset's *parent* contents instead means the path is one level too high.
* `touch /data/atriumdb/_wtest` — deliberately tries to write. It **must** fail
  with `Read-only file system`. Failure is the pass condition: it proves the
  `:ro` flag is in effect and no container can modify patient data. If it
  succeeds, the mount lost its `:ro` — stop and fix that before going further
  (and delete the stray `_wtest` file it created).

Then prove both backends are actually reachable:

```bash
docker compose exec atriumdb-api python -c \
  "import urllib.request;print(urllib.request.urlopen('http://localhost:8000/health/ready').read())"
```

`/health/ready` checks each backend independently and always returns HTTP 200 —
it is a diagnostic, and an orchestrator that restarted the container on a
transient API outage would make things worse. Read the body:

| Response | Meaning |
|---|---|
| `{"metadata": "ok", "data_api": "ok"}` | both backends reachable — UAT is fully configured |
| `{"metadata": "ok", "data_api": "not configured"}` | expected for a local SQLite deployment |
| `{"metadata": "unavailable: ...", ...}` | MariaDB unreachable, or credentials rejected |
| `{..., "data_api": "unavailable: AuthError"}` | Auth0 rejected the client — wrong secret, or the client is not authorised for that audience |
| `{..., "data_api": "unavailable: ValueError"}` | Auth0 issued a token but the API refused it, or `ATRIUMDB_API_URL` is wrong. A 404 here usually means a missing version segment in the URL |

`/health` (no `/ready`) stays a pure liveness probe that touches neither backend,
which is what compose should gate `depends_on` on.

Then run the route checks in §5. Do this **before** starting the other three
services — a dataset-path mistake surfaces here in seconds, versus as an opaque
502 through two proxies later.

### Step 6 — start the rest

```bash
docker compose up -d
docker compose ps        # all four Up; postgres and backend healthy
```

* `docker compose up -d` — same command as step 5 with no service named, so it
  applies to all four. `atriumdb-api` is already running and its configuration
  has not changed, so compose leaves it alone rather than restarting it; the
  other three (`postgres`, `backend`, `frontend`) are created and started, in
  `depends_on` order. This is also the command that publishes the host port.
* `docker compose ps` — one row per container with its state and published
  ports. You want all four `Up`, and `postgres` and `backend` reporting
  `(healthy)` — they define healthchecks, so `Up (starting)` for a few seconds is
  normal. A container in `Exited` or restart-looping is the one to pull logs
  from: `docker compose logs <service>`.

The dashboard is then on `http://<server>:${HTTP_PORT}` (default 8080).

### Step 7 — end-to-end check

Log into the dashboard in a browser and run one cohort query through to
statistics. That is the only check that exercises the full chain
(browser → nginx → backend → this container → SDK → dataset). Watch both logs
while you do it:

```bash
docker compose logs -f backend atriumdb-api
```

* Naming two services interleaves both log streams, each line prefixed with its
  service name. Start it *before* you click in the browser, then watch a single
  cohort query travel: the `backend` request line first, then the matching
  `atriumdb-api` line. A request that appears in `backend` but never in
  `atriumdb-api` is a networking or URL problem; one that appears in both but
  errors in `atriumdb-api` is a dataset or SDK problem — which tells you which
  log to dig into. Ctrl-C when done; nothing stops.

## 4. Operating the container

Every command runs from `~/sickkids/SickKids_Dashboard`.

| Task | Command |
|---|---|
| Status | `docker compose ps` |
| Follow logs | `docker compose logs -f atriumdb-api` |
| Check both backends | `docker compose exec atriumdb-api python -c "import urllib.request;print(urllib.request.urlopen('http://localhost:8000/health/ready').read())"` |
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

### Rotating a credential

Neither the MariaDB password nor the Auth0 secret is baked into the image, so a
rotation is a file edit and a restart — no rebuild:

```bash
cd ~/sickkids/SickKids_Dashboard
$EDITOR ./atriumdb-api.env
docker compose up -d atriumdb-api      # recreates with the new environment
```

`docker compose restart` is **not** enough: it restarts the process with the
container's existing environment, which was fixed when the container was created.
`up -d` notices the changed `env_file` and recreates it.

The Auth0 token itself needs no attention — it is held only in memory, re-minted
near expiry, and never written to disk.

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

## 5. Verifying the routes

The dashboard calls five routes on this service. The image has Python but
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
body, and `POST /cohorts/timeseries` with a `TimeSeriesRequest`. Both **require a
non-empty `X-Request-ID`** and return 400 without one — that is deliberate, not a
fault. Both return **503** when `ATRIUMDB_API_URL` is unset, which is also
deliberate: the routes exist but have no backend to serve them.

| Dashboard call | Served by | SDK it uses | Response it parses |
|---|---|---|---|
| `POST /cohorts` | `cohort_endpoints.post_cohorts` | metadata | `MrnCohortResponse` |
| `POST /cohorts/statistics` | `statistics_endpoints.post_cohort_statistics` | **data** | `AggregateStatisticsResponse` |
| `POST /cohorts/timeseries` | `timeseries_endpoints.post_cohort_timeseries` | **data** | `TimeSeriesResponse` |
| `GET /measures/hours` | `measures_endpoints.get_measure_total_hours` | metadata | bare list of per-measure hour rows |
| `GET /measures/` | upstream `measures_endpoints.search_measures` | metadata | `{measure_id: measure_info}` |

The SDK column is worth reading when a route misbehaves: the two marked **data**
are the only ones that touch the API and Auth0, so a failure confined to those
two is an API-side problem, and one affecting the others is a metadata-side
problem. `/health/ready` reports the same split.

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

Configuration problems now surface **at startup**, not on the first request:
`check_configuration()` runs at import and refuses to start, naming what is
missing. If the container is serving at all, the configuration parsed.

| Symptom | Cause | Fix |
|---|---|---|
| Container exits at startup, `ConfigError: ... is missing required environment variable(s): X, Y` | exactly what it says | Add them to `atriumdb-api.env` (step 2.5) or `docker-compose.yml`; every missing name is listed at once |
| `ConfigError: ATRIUMDB_API_URL is set ... no way to authenticate` | API URL given without the `AUTH0_*` set | Supply the Auth0 values, or unset `ATRIUMDB_API_URL` to run without the data backend |
| `ATRIUMDB_DATASET_LOCATION is not set` at startup | env var missing from the service | It is set in `docker-compose.yml`; confirm you are on the deploy branch of the dashboard repo. Required in `mariadb` mode too — see §1 |
| `No Dataset found at location /data/atriumdb` | mount points at the wrong directory, or one level too high/low | `docker compose exec atriumdb-api ls -l /data/atriumdb` — must show `meta/` and `tsc/` |
| `/data/atriumdb` is empty | `ATRIUMDB_DATASET_PATH` does not exist on the host; Docker created an empty dir | Fix the path in `.env`, then `docker compose up -d atriumdb-api` |
| `Permission denied` reading `meta/index.db` | dataset not readable by the Docker user | `chmod`/`chown` on the host, or run compose as a user that can read it |
| `attempt to write a readonly database` | something tried to write through the read-only mount | A read path should never do this — capture the traceback; do **not** drop `:ro` to make it go away |
| `mrn column is using an INTEGER type` | dataset predates the TEXT-MRN schema | Schema upgrade writes, so it cannot happen against a `:ro` mount — upgrade a writable copy separately, then redeploy |
| `OSError: AtriumSDK is not currently supported on macOS` | ran the SDK outside the container | Always go through Docker |
| Backend logs `AtriumDB unreachable at http://atriumdb-api:8000` | this container is down or crashed at import | `docker compose ps`, then `docker compose logs atriumdb-api` |
| First request after start is slow, later ones fast | expected — the SDK is built once and cached | none |
| `/cohorts/statistics` or `/cohorts/timeseries` returns **503** | `ATRIUMDB_API_URL` not configured, or a token could not be obtained | Check `/health/ready`; the detail names which |
| 503 with `AuthError` | Auth0 rejected the client | `access_denied` = not authorised for that audience; `unauthorized_client` = client_credentials not enabled; `invalid_client` = wrong id or secret. The log carries Auth0's own message |
| Auth works, measures read, then statistics hangs | `wss://` blocked while `https://` is open | Block transfers use the websocket. Open `wss://` to the API host — see §2 |
| `404` on every API call, but the token is fine | `ATRIUMDB_API_URL` missing its version segment | The SDK appends bare endpoint names; the prefix must be in the base URL (`https://host/api/v1`) |
| Statistics returns 200 with every patient excluded | the measure or the patients exist in MariaDB but not in the UAT dataset | Expected and by design — the two backends can differ in coverage. Check the exclusion log for the reason per patient |
| A cohort comes back smaller than expected, many `MRN_NOT_FOUND` | possibly genuine, possibly a transient API fault | The SDK cannot distinguish a 404 from a server error when resolving MRNs, so an API problem looks like unknown MRNs. A sudden spike in that count means suspect the API, not the data |
| `ValueError: The connection is already borrowed` | MariaDB pooling left on | Should not occur: `get_meta_sdk` passes `no_pool=True` precisely to prevent it. If it appears, that argument was lost |
| Long statistics query blocks other requests | should no longer happen | The routes are plain `def`, so FastAPI runs them in its threadpool and `/health` stays responsive. Block transfers are still serialised by a lock, so concurrent statistics requests queue on the websocket — expected, and not the same as blocking everything |

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

Use `/health`, not `/health/ready`, for this. `/health` answers whether the
process is serving; `/health/ready` reaches out to MariaDB and the API, so
gating `depends_on` on it would mean a transient API outage stops the whole
stack from coming up. `/health/ready` is for humans and monitoring.

That change belongs to the dashboard repo, not this one.

## 8. Standing caveat

`tests/mock_api/` is a **test fixture** — no authentication, no error envelope,
no configuration surface — and this deployment serves it as the production API
layer. Note this is about the API *this container exposes* to the dashboard
backend, not the upstream AtriumDB API it now consumes for waveform data; those
are opposite directions and only the former is a fixture. That is defensible on a closed research server where the only client is
the backend on a private bridge network, but it is a choice, not an accident. If
a real AtriumDB server exists elsewhere, the better move is to point the
dashboard's `ATRIUMDB_URL` at it and delete the `atriumdb-api` service from
compose entirely — the dashboard needs no code change, since the address is
configuration.

If this path is kept long term, move `tests/mock_api/` to a real package (e.g.
`atriumdb/server/`) so production stops depending on `tests/` remaining in the
image.
