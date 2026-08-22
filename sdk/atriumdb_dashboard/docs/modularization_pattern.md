# Modularization Pattern: extracting dashboard work out of `atriumdb`

How the dashboard was lifted out of the SDK so that `atriumdb/` stays byte-identical to
upstream `main`. Written to be re-applied to another branch carrying similar changes.

**The invariant.** After the restructure this must print nothing:

```bash
git diff main --name-only -- sdk/atriumdb/ sdk/tests/mock_api/ .gitignore sdk/pyproject.toml
```

Everything the dashboard adds lives under `sdk/atriumdb_dashboard/` (code) or
`sdk/tests/atriumdb_dashboard/` (tests). The dependency runs one way only: the dashboard
imports from `atriumdb`; `atriumdb` never imports the dashboard.

---

## 1. Target layout

```
sdk/
├── atriumdb/                       ← untouched, byte-identical to upstream
├── atriumdb_dashboard/
│   ├── pyproject.toml              ← own distribution: atriumdb-dashboard
│   ├── __init__.py                 ← public re-exports; does NOT import .api
│   ├── schemas.py                  ← pydantic models (no SDK imports)
│   ├── locations.py                ← domain lookups validated against the DB
│   ├── queries.py                  ← raw SQL + row shaping
│   ├── cohort_resolver.py          ← orchestration + api/local entry point
│   ├── api/                        ← FastAPI surface, optional import
│   │   ├── __init__.py
│   │   ├── <feature>_endpoints.py  ← router + its own DI provider
│   │   └── app.py                  ← mount_dashboard() / create_dashboard_app()
│   ├── docker/
│   │   ├── Dockerfile
│   │   ├── Dockerfile.dockerignore ← BuildKit per-Dockerfile ignore
│   │   └── docker-run-dataset.sh
│   └── docs/
└── tests/
    ├── mock_api/                   ← untouched
    └── atriumdb_dashboard/         ← mirrors the package name
```

Test directory name mirrors the package name exactly.

---

## 2. The four coupling types, and how each is removed

### 2a. A method added to `AtriumSDK` → a free function taking `sdk`

A method needs `self` only for attribute access. If it uses nothing private, it works
unchanged as a function whose first parameter is the SDK.

```python
# before                                    # after
sdk.dashboard_resolve_cohort(req, rid)      resolve_cohort(sdk, req, rid)
```

Check first that every SDK member it touches already exists on `main` — if it needs a *new*
SDK method, that is a real coupling and needs its own decision.

Removing the method also removes the SDK's top-level `import pydantic` chain, which
otherwise makes `import atriumdb` fail wherever pydantic is not installed.

### 2b. A method added to `SQLHandler` + both backends → one function in `queries.py`

The most valuable extraction. Adding an `@abstractmethod` to `SQLHandler` is a breaking
change for any out-of-tree handler subclass and conflicts on every upstream merge.

The per-backend implementations are usually identical apart from the connection manager, so
they collapse into one function. Reach the database through the upstream backend-agnostic
accessor — **not** the backend-specific ones:

```python
def _handler_connection(sdk):
    if getattr(sdk, "metadata_connection_type", None) == "api":
        raise ValueError("... requires direct database access ...")
    return sdk.sql_handler.connection(begin=False)   # abstract on SQLHandler; both implement
```

`sql_handler` is a public attribute (used ~76× inside `atrium_sdk.py`) and
`connection(begin)` is `@abstractmethod` on `SQLHandler`, so any conforming handler works and
nothing new is required of `atriumdb`. Do not reach for `sqlite_db_connection` /
`maria_db_connection` by name.

One SQL string serves both backends: same schema, both `?` paramstyle, ANSI-only constructs.

**Before adding SQL, grep for an existing handler method.** `select_unit(name=...)` already
existed — concrete on the base class, so both backends inherit it — which removed the need
for a hand-written unit lookup entirely.

### 2c. An edit to a private SDK helper → restate it locally

`AtriumSDK._request` rebuilds its header dict, so it cannot carry `X-Request-ID`. Rather than
patch it, the dashboard issues its own request, restating URL construction, the token-refresh
check, and non-200 handling.

This is the one place duplication is accepted, and it carries a maintenance cost: **if
upstream changes `_request`'s auth or refresh behaviour, the local copy must be updated to
match.** Note it in the code where it lives.

### 2d. An edit to a shared app/config file → compose at runtime

Never edit the host's `app.py`. Give the router its own dependency provider and mount it:

```python
def mount_dashboard(app, prefix="/cohorts"):
    app.include_router(router, prefix=prefix)
    return app
```

Tests call `mount_dashboard(app)` at import time against the untouched upstream app, and
override the dashboard's own provider, not one borrowed from `tests/`.

---

## 3. Validation that needs the database

A pydantic `field_validator` runs at model construction with no SDK and no connection, so it
cannot consult the database. Anything DB-backed must move to resolve time:

1. Drop the validator from `schemas.py`; the field accepts any value.
2. Validate in the resolver, where the SDK is in hand.
3. Raise a domain error subclassing `ValueError` (so `except ValueError` still catches).
4. Catch it in the endpoint and re-raise as `HTTPException(422)`.

The HTTP contract is unchanged; a *direct* SDK caller now sees the error at resolve time
rather than at construction. Update the model-level tests accordingly.

---

## 4. Packaging

`atriumdb_dashboard/pyproject.toml` defines a separate distribution. Because the package sits
in the same directory as its `pyproject.toml`, map it explicitly:

```toml
[tool.setuptools]
packages = ["atriumdb_dashboard", "atriumdb_dashboard.api"]

[tool.setuptools.package-dir]
"atriumdb_dashboard"     = "."
"atriumdb_dashboard.api" = "api"
```

Without `package-dir`, setuptools packages `api/`, `docs/`, and `docker/` as top-level
modules. Verify with a real build — the wheel must contain only `.py` files:

```bash
pip wheel --no-deps -w /tmp/wh atriumdb_dashboard/
python -c "import zipfile;print(zipfile.ZipFile('/tmp/wh/<name>.whl').namelist())"
```

Install order (`sdk/pyproject.toml` stays untouched):

```dockerfile
RUN pip install --no-cache-dir -e ".[testing]"
RUN pip install --no-cache-dir -e "./atriumdb_dashboard[testing]"
```

The second resolves `atriumdb>=X` against the editable install; nothing comes from PyPI.

**Known wart:** `sdk/pyproject.toml` uses `packages.find where=['.']`, which auto-discovers
`atriumdb_dashboard` (and `tests`, already true on `main`), so a non-editable `atriumdb`
build bundles it too. Harmless for editable dev workflows. Fixing it costs one `exclude` line
in `sdk/pyproject.toml`, which breaks the zero-diff invariant — a deliberate trade.

---

## 5. Renaming the test directory

If the package is renamed, the test directory follows, and three references go stale that no
test failure will catch:

- `docker/Dockerfile` → the `CMD` pytest path
- `docker/Dockerfile.dockerignore` → the `test_datasets/` exclusion
- `docs/*.md` → layout diagrams

```bash
grep -rn "tests/<old-name>" atriumdb_dashboard/ tests/
```

---

## 6. Checklist

1. `mkdir` the package; `git mv` modules in (preserves history).
2. `git checkout main -- <every upstream file the branch touched>`.
3. Rewrite imports to the new module paths.
4. Apply 2a–2d for each coupling.
5. Give routers their own DI provider; add `mount_dashboard`.
6. Write `pyproject.toml`; verify by building a wheel and listing its contents.
7. Move tests; update imports and any `Path(__file__).parent` that shifted depth.
8. Confirm the invariant in §1 prints nothing.
9. Run the suite in Docker (`AtriumSDK.__init__` refuses to run on macOS).
10. If a test fails, check it against the pre-restructure commit in a worktree before
    assuming the restructure caused it:
    `git worktree add /tmp/orig <sha> && docker run --rm -v /tmp/orig/sdk:/sdk <image> pytest <test>`
