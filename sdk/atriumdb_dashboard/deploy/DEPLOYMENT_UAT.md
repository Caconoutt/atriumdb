# Deploying the AtriumDB API container — UAT

How to stand up the `atriumdb-api` service against **UAT**, where both the
metadata and the waveform data live on servers someone else operates.

For the self-contained deployment that bind-mounts a local dataset, and for
operations, route verification and troubleshooting once the stack is up, see
[`DEPLOYMENT.md`](./DEPLOYMENT.md).

---

## What makes UAT different

The container opens **two** SDK instances, and in UAT neither of them touches a
local dataset:

| | metadata SDK | data SDK |
| --- | --- | --- |
| serves | `POST /cohorts`, `GET /measures/hours` | `POST /cohorts/statistics`, `POST /cohorts/timeseries` |
| reads | **MariaDB**, read-only account | the **AtriumDB API**, over an Auth0 token |
| configured by | `ATRIUMDB_MARIA_*` | `ATRIUMDB_API_URL` + `AUTH0_*` |

Each endpoint uses exactly one of them, so a failure tells you which side is
unwell.

**There is no dataset to prepare, copy, or mount.** That is the single biggest
difference from the local deployment: no `prepare_dataset.py` run, no `rsync`, no
read-only bind mount, no `ATRIUMDB_DATASET_PATH`. Everything the container reads
arrives over the network.

```
  host :8080 ──► frontend      nginx — serves the SPA, proxies /api
                    │
                 backend       FastAPI — the dashboard API
                    ├────────► postgres        dashboard's own store
                    └────────► atriumdb-api    ◄── THIS REPO
                                    │
                                    ├──► MariaDB          (metadata, read-only)
                                    └──► AtriumDB API     (waveforms, https + wss)
                                             ▲
                                             └── Auth0 issues the token
```

Only `frontend` publishes a host port. `atriumdb-api` is reachable only from
`backend`, at `http://atriumdb-api:8000`.

### One variable that looks wrong but is required

`ATRIUMDB_DATASET_LOCATION` must still be set in `mariadb` mode. The SDK
constructor builds a file handler before it looks at the connection type, so it
demands a file location — but the handler only stores the string, and nothing
reads a `.tsc` file through the metadata SDK. **The path is never opened and does
not need to exist.** Point it anywhere inert:

```yaml
    ATRIUMDB_DATASET_LOCATION: /tmp/atriumdb-unused
```

---

## Prerequisites

On the server:

* Docker Engine with the Compose plugin (`docker compose version` must work).
  Compose v1 (hyphenated `docker-compose`) is not supported by these files.
* `git`.
* ~1.4 GB of disk for the image, plus the Postgres volume. No dataset storage.
* The user running compose must be in the `docker` group, or every command needs
  `sudo`. Pick one and stay consistent — images built under `sudo` are not
  visible to a rootless daemon and vice versa.

Outbound network **from the container** to:

| Destination | Protocol | Used for |
| --- | --- | --- |
| Auth0 tenant | `https://` | minting the token |
| AtriumDB API | `https://` **and** `wss://` | metadata over REST, block transfers over the websocket |
| MariaDB host | its port, 3306 by default | all metadata queries |

> **`wss://` is a separate firewall rule from `https://`, and it is the one that
> gets forgotten.** With only HTTPS open the container authenticates fine, reads
> measures fine, and then hangs on the first statistics request — because that is
> the first thing that moves blocks.

Credentials, before you start — see [Step 2](#step-2--place-the-credentials) for
what to ask for.

---

## Step 1 — clone both repos as siblings

```bash
ssh <user>@<server>
mkdir -p ~/sickkids && cd ~/sickkids

git clone <atriumdb-remote> atriumdb
git clone <dashboard-remote> SickKids_Dashboard
```

The directory names matter — the dashboard's compose file resolves
`context: ../atriumdb/sdk` relative to itself. The final layout must be:

```
~/sickkids/
├── SickKids_Dashboard/     ← run every compose command from here
└── atriumdb/               ← this repo
```

Check out the branch carrying the UAT changes in **both** repos:

```bash
cd ~/sickkids/atriumdb && git checkout <branch> && git log --oneline -1
cd ~/sickkids/SickKids_Dashboard && git checkout <branch> && git log --oneline -1
```

---

## Step 2 — place the credentials

### What to ask for

| From | What |
| --- | --- |
| whoever owns the MariaDB | host, port, database, username, password — ask explicitly for a **read-only** account |
| whoever owns the API | Auth0 client id, client secret, audience, tenant — **and the API base URL** |

Two things worth insisting on:

* **Read-only on MariaDB.** Nothing the dashboard does writes. A read-only grant
  turns a mistake into a permissions error instead of a schema change on a shared
  server. The account needs `SELECT` on `settings`, `measure`, `block_index`,
  `encounter`, `bed`, `unit`, `patient`, `patient_history`. `settings` is the one
  people forget — the SDK constructor reads it before serving anything.
* **Ask for the API URL; do not derive it.** An Auth0 token carries `iss` and
  `aud`, neither of which has to be the API's address. `AUTH0_AUDIENCE` and
  `ATRIUMDB_API_URL` are different values that often look similar.

### Put them in a file outside the build context

```bash
cd ~/sickkids/SickKids_Dashboard
cp ../atriumdb/sdk/atriumdb_dashboard/.env.example ./atriumdb-api.env
$EDITOR ./atriumdb-api.env
chmod 600 ./atriumdb-api.env
```

Fill in:

```ini
ATRIUMDB_METADATA_CONNECTION_TYPE=mariadb
ATRIUMDB_DATASET_LOCATION=/tmp/atriumdb-unused   # required, never opened

ATRIUMDB_MARIA_HOST=...
ATRIUMDB_MARIA_PORT=3306
ATRIUMDB_MARIA_USER=...
ATRIUMDB_MARIA_PASSWORD=...
ATRIUMDB_MARIA_DATABASE=...

ATRIUMDB_API_URL=https://host/api/v1             # include the version segment
AUTH0_TENANT=your-tenant.us.auth0.com
AUTH0_CLIENT_ID=...
AUTH0_CLIENT_SECRET=...
AUTH0_AUDIENCE=...
```

> **`ATRIUMDB_API_URL` must carry the full prefix, version segment and all.** The
> SDK appends bare endpoint names (`measures/`, `sdk/blocks`) to it and has no
> separate setting for a version. Get it wrong and every request 404s, with a
> valid token. If the docs are at `https://host/api/docs` and routes look like
> `/v1/measures`, the value is `https://host/api/v1`.

Three rules, none optional:

* **Never `-e` on a command line.** `docker run -e PASSWORD=…` puts the value in
  the host's process list and in shell history.
* **Never inside the build context.** `sdk/` is the build context and the
  Dockerfile ends in `COPY . .`, so a `.env` there is baked into an image layer —
  and deleting it afterwards does not remove it from the layer. The
  `.dockerignore` excludes `**/.env` so a slip is harmless, but keeping the real
  file outside the context is the actual protection.
* **If a credential was ever inside an image, rotate it.** Excluding a file from
  future builds does nothing about images already built.

---

## Step 3 — point the service at the credentials

In `SickKids_Dashboard/docker-compose.yml`, the `atriumdb-api` service:

```yaml
  atriumdb-api:
    build:
      context: ../atriumdb/sdk         # ← the two repos must be siblings
      dockerfile: atriumdb_dashboard/docker/Dockerfile
    env_file:
      - ./atriumdb-api.env
```

Compared with the local deployment, two things **go away**: the
`ATRIUMDB_DATASET_LOCATION` entry under `environment:` (it now comes from
`atriumdb-api.env`), and the whole `volumes:` block — there is no dataset to
mount.

> **The `dockerfile:` key is required.** The Dockerfile lives inside the
> dashboard package rather than at the build-context root, so a compose file
> carrying only `context:` fails with "Dockerfile not found". The context stays
> `../atriumdb/sdk` — the build needs the whole `sdk/` tree.

Then the dashboard's own configuration, which is a different file:

```bash
cd ~/sickkids/SickKids_Dashboard
cp .env.docker.example .env
$EDITOR .env
```

`ATRIUMDB_DATASET_PATH` is no longer used by this service; if the compose file
still interpolates it anywhere, leave it set to any existing directory so
substitution does not fail. Set `JWT_SECRET` to a generated value and change
`POSTGRES_PASSWORD` — neither affects this container, but the stack is not safe
to expose without them. Raise `ATRIUMDB_STATISTICS_TIMEOUT_SECONDS` if large
cohorts time out; over UAT every window crosses the network, so this budget
matters more than it did locally.

Keeping the AtriumDB credentials in `atriumdb-api.env` rather than in `.env` is
deliberate: `.env` is read by all four services, and only this one needs them.

---

## Step 4 — build

```bash
cd ~/sickkids/SickKids_Dashboard
docker compose config        # check .env substitution before a multi-minute build
docker compose build
```

`docker compose config` renders the compose file with every `${VAR}` substituted
and exits. It catches an **unset or empty** variable; it does **not** check that
paths exist or that the atriumdb repo is where `context:` says. It starts
nothing.

First build takes several minutes — compiling the MariaDB C extension dominates.
Later builds reuse the layer cache. `docker compose build atriumdb-api` limits it
to this service.

---

## Step 5 — start this service alone and prove it

```bash
docker compose up -d atriumdb-api
docker compose logs -f atriumdb-api        # Ctrl-C detaches; the container keeps running
```

Two lines to look for, in order:

```
Dashboard configuration: metadata: mariadb ro@db.host:3306/atrium | data api: https://host/api/v1 (auth0 client_credentials)
Uvicorn running on http://0.0.0.0:8000
```

The first is the container reporting what it resolved. **Read it** and confirm it
names the backends you meant. Secrets are redacted, so a password appearing there
is a bug worth reporting.

Configuration is validated at import, so a bad value stops the container
**before** uvicorn starts: a `ConfigError` naming a variable, and no "Uvicorn
running" line, means exactly what it says. Every missing variable is listed at
once rather than one per restart.

Then prove both backends are actually reachable:

```bash
docker compose exec atriumdb-api python -c \
  "import urllib.request;print(urllib.request.urlopen('http://localhost:8000/health/ready').read())"
```

`/health/ready` checks each backend independently and always returns HTTP 200 —
it is a diagnostic, not a gate. Read the body:

| Response | Meaning |
| --- | --- |
| `{"metadata": "ok", "data_api": "ok"}` | both reachable — this is the goal |
| `{"metadata": "unavailable: ...", ...}` | MariaDB unreachable, or the account was rejected |
| `{..., "data_api": "unavailable: AuthError"}` | Auth0 rejected the client — wrong secret, or not authorised for that audience |
| `{..., "data_api": "unavailable: ValueError"}` | Auth0 issued a token but the API refused it, or `ATRIUMDB_API_URL` is wrong. A 404 here usually means a missing version segment |
| `{..., "data_api": "not configured"}` | `ATRIUMDB_API_URL` is unset — statistics and time-series will 503 |

Do this **before** starting the other three services. A rejected credential shows
up here in seconds, rather than as an opaque 502 through two proxies once
everything is running.

---

## Step 6 — start the rest

```bash
docker compose up -d
docker compose ps        # all four Up; postgres and backend (healthy)
```

`atriumdb-api` is already running and its configuration has not changed, so
compose leaves it alone and creates the other three in `depends_on` order. This
is also the command that publishes the host port.

The dashboard is then on `http://<server>:${HTTP_PORT}` (default 8080).

---

## Step 7 — end-to-end check

Log in and run one cohort query through to statistics. That is the only check
that exercises the whole chain — browser → nginx → backend → this container →
MariaDB **and** the AtriumDB API. Watch both logs while you do it:

```bash
docker compose logs -f backend atriumdb-api
```

Start it before you click, then follow one query through. A request that appears
in `backend` but never in `atriumdb-api` is a networking or URL problem; one that
appears in both and errors in `atriumdb-api` is a backend problem — and which
route failed tells you which of the two:

| Route | Exercises |
| --- | --- |
| `POST /cohorts`, `GET /measures/hours` | MariaDB only |
| `POST /cohorts/statistics`, `POST /cohorts/timeseries` | the AtriumDB API and Auth0 |

A cohort that resolves but statistics that fail means MariaDB is fine and the API
side is not.

---

Once the stack is up, [`DEPLOYMENT.md`](./DEPLOYMENT.md) covers operating the
container, verifying individual routes, rotating credentials, and
troubleshooting.
