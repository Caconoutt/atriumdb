# AtriumDB query sandbox (Docker)

A throwaway container for poking at a real AtriumDB dataset on the remote
server — raw SQL against `meta/index.db`, or the SDK/CLI. The dataset is mounted
**read-only**, so nothing in here can modify it.

Files:

| file | what it is |
| --- | --- |
| `Dockerfile` | builds `libTSC.so` from `tsc-lib/`, then installs the SDK |
| `docker-compose.yml` | wires up the dataset mount and the env vars |
| `.env.example` | template — copy to `.env` and set the dataset path |
| `queries/EXAMPLES.md` | copy-paste query snippets |
| `queries/query_template.py` | edit the marked block, run it |
| `out/` | anything you write here appears on the host |

---

## 1. Get onto the server

```bash
ssh <user>@<server>
```

Check Docker is there and you can use it:

```bash
docker --version
docker compose version
docker ps            # if this says "permission denied", see Troubleshooting
```

## 2. Get the branch

First time:

```bash
git clone <repo-url> atriumdb
cd atriumdb
git checkout docker-query-sandbox
```

Already cloned:

```bash
cd ~/atriumdb
git fetch origin
git checkout docker-query-sandbox
git pull
```

## 3. Point it at the dataset

```bash
cd docker
cp .env.example .env
```

Edit `.env` and set `DATASET_LOCATION` to the absolute path of the dataset
directory on the server — the one containing `meta/index.db` and `tsc/`:

```bash
# sanity check before you continue
ls "$(grep '^DATASET_LOCATION=' .env | cut -d= -f2-)/meta/index.db"
```

While you're there, set `LOCAL_UID` / `LOCAL_GID` to your own (`id -u`, `id -g`)
so files written to `out/` come back owned by you.

`.env` is gitignored — the real path never gets committed.

## 4. Build

```bash
docker compose build
```

Takes a few minutes the first time (it compiles `libTSC.so` from source so the
binary matches the server's architecture, then installs the SDK). Rebuilds are
cached unless `tsc-lib/` or `sdk/` changed.

## 5. Run

Interactive shell — this is the normal way to use it:

```bash
docker compose run --rm atriumdb
```

You land in `/workspace` with:

- `/data/atriumdb` — the dataset, read-only
- `/workspace/queries` — the query scripts, live-mounted from `docker/queries`
- `/workspace/out` — write results here, they show up in `docker/out` on the host

Quick smoke test once inside:

```bash
python -c "
import sqlite3
conn = sqlite3.connect('/data/atriumdb/meta/index.db')
print(conn.execute('SELECT COUNT(*) FROM measure').fetchone())
"
```

Then work through `queries/EXAMPLES.md`, or:

```bash
python queries/query_template.py
```

Editing `queries/*.py` on the host takes effect immediately — no rebuild, no
restart.

One-off without an interactive shell:

```bash
docker compose run --rm atriumdb python queries/query_template.py
docker compose run --rm atriumdb sqlite3 -readonly /data/atriumdb/meta/index.db ".tables"
```

Long-running query, detached from your ssh session (so it survives a dropped
connection):

```bash
nohup docker compose run --rm -T atriumdb python queries/query_template.py \
  > out/run.log 2>&1 &
tail -f out/run.log
```

## 6. Clean up

```bash
docker compose down                    # remove stray containers
docker image rm atriumdb-query-sandbox # remove the image entirely
docker compose build --no-cache        # force a full rebuild
```

---

## Troubleshooting

**`DATASET_LOCATION` variable is not set** — you're not running from the
`docker/` directory, or you never copied `.env.example` to `.env`.

**`No Dataset found at location /data/atriumdb`** — `DATASET_LOCATION` points at
the wrong level. It must be the directory *containing* `meta/index.db`, not
`meta/` itself and not the `index.db` file.

**`unable to open database file`** — usually permissions on the host: your user
needs read access to the dataset directory. Check with
`ls -l "$DATASET_LOCATION/meta/index.db"`, and confirm `LOCAL_UID`/`LOCAL_GID`
in `.env` match a user that can read it.

**`attempt to write a readonly database`** — expected. The mount is read-only on
purpose. Write output to `/workspace/out` instead.

**`OSError: ... libTSC.so: cannot open shared object file` or `wrong ELF class`**
— the SDK is finding the wrong shared library. The image compiles its own; if
you see this, the build stage failed silently, so rerun
`docker compose build --no-cache` and watch the `tsc-builder` stage. (The
`libTSC.so` committed in `sdk/bin/` is aarch64 and will not load on an x86_64
server — the image build is what fixes that.)

**`permission denied ... docker.sock`** — your user isn't in the `docker` group.
Either ask an admin for `sudo usermod -aG docker $USER` (then log out and back
in), or prefix commands with `sudo` — but if you use `sudo`, set `LOCAL_UID` /
`LOCAL_GID` in `.env` to your own ids so `out/` doesn't fill up with root-owned
files.

**Files in `out/` are owned by root** — `LOCAL_UID`/`LOCAL_GID` in `.env` don't
match you. Fix them, then `sudo chown -R $(id -u):$(id -g) out`.
