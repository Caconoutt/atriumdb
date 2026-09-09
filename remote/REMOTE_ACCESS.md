# Remote AtriumDB access over Auth0 (M2M)

Connect to an AtriumDB API server that someone else hosts, authenticating with
Auth0 machine-to-machine credentials, and read data through the SDK as if it
were local.

## How the pieces fit

Two separate services are involved. Auth0 issues tokens and holds no data; the
AtriumDB API holds the data and issues nothing.

```
  1. you ──── client_id + secret ───→ Auth0            "prove who you are"
          ←──── access_token (JWT) ───

  2. you ── Authorization: Bearer ──→ AtriumDB API     "here's my proof"
          ←──────── your data ───────
```

Your client secret only ever goes to step 1. The API verifies the token's
signature against Auth0's public keys and never sees the secret.

What runs where:

```
  [provider's machine]                    [your container]
  FastAPI + local SDK + dataset           test_connection.py
    GET /measures/            ←──────────   AtriumSDK(metadata_connection_type="api")
    GET /sdk/blocks                         └─ returns python objects, not JSON
    WS  /sdk/blocks/ws
```

There is **no web server on your side**. The SDK is a library: it makes the HTTP
and websocket calls internally and hands back dicts and numpy arrays. The only
FastAPI in this picture is the provider's.

## Where the six values go

Four of them never touch the SDK. `AtriumSDK.__init__` accepts only `api_url`,
`token`, `refresh_token` and `validate_token` — there is no `client_id`
parameter.

| value | env var | used by |
| --- | --- | --- |
| client_id | `AUTH0_CLIENT_ID` | the Auth0 token request |
| client_secret | `AUTH0_CLIENT_SECRET` | the Auth0 token request |
| audience | `AUTH0_AUDIENCE` | the Auth0 token request; becomes the token's `aud` claim |
| grant_type | `AUTH0_GRANT_TYPE` | the Auth0 token request (`client_credentials`) |
| auth0_tenant | `AUTH0_TENANT` | the host the token request is sent to |
| auth0_audience | `AUTH0_AUDIENCE` | same value as `audience` in this flow |
| — | `ATRIUMDB_API_URL` | **the SDK.** Not supplied by Auth0 — ask the provider |

`AUTH0_AUDIENCE` and `ATRIUMDB_API_URL` are different things and are easy to
confuse. The audience is an identifier Auth0 stamps into the token. The API URL
is where the server listens. They are often not the same string.

## Finding the API URL

The token does not contain it. An Auth0 token carries `iss` (the tenant) and
`aud` (the audience identifier); neither is required to be the API's base URL,
so completing the Auth0 step does not reveal it.

The reliable answer is to **ask the provider** — it is one message, and it also
settles whether the API exposes a `/cohorts` endpoint.

Failing that, the audience is a reasonable first guess (convention is to use the
real URL as the identifier) and it is testable:

```bash
python3 remote/discover_api_url.py                    # starts from AUTH0_AUDIENCE
python3 remote/discover_api_url.py https://host/api   # or an explicit guess
```

It probes the usual prefixes (``, `/api`, `/api/v1`, `/v1`, `/atriumdb`) against
`/openapi.json`, `/docs`, `/auth/cli/code` and `/measures/`, and reports what
answers. `/openapi.json` is the decisive one — FastAPI serves it by default and
it lists every route; seeing `/sdk/blocks` in there confirms both the base URL
and that the host really is an AtriumDB server.

Reading the results:

- **200 on `/openapi.json` with `/sdk/blocks` present** — that base URL is your
  `ATRIUMDB_API_URL`.
- **401 or 403** — the route *exists* and is protected. Still a hit; the base URL
  is probably right and the token is the problem.
- **404 everywhere** — the base URL is not derivable from the audience. Ask.

## Setup

```bash
cp remote/.env.example remote/.env
$EDITOR remote/.env          # fill in the five Auth0 values + ATRIUMDB_API_URL
```

`remote/.env` is gitignored. Keep the real secret out of `.env.example`.

## Stage 1 — mint a token (runs on macOS)

`auth0_token.py` does not import the SDK, so it runs on your laptop.

```bash
pip3 install requests python-dotenv     # if not already present
python3 remote/auth0_token.py --decode
```

Success prints the token on stdout and its claims on stderr. Check two things:

- **`aud`** matches the API you expect
- **`expires`** is roughly 24 hours out

If this fails, the problem is entirely between you and Auth0 — nothing to do
with AtriumDB yet. See Troubleshooting.

You can also confirm the API accepts it without any Python:

```bash
TOKEN=$(python3 remote/auth0_token.py)
curl -s -o /dev/null -w '%{http_code}\n' -H "Authorization: Bearer $TOKEN" \
  "$ATRIUMDB_API_URL/measures/"
```

`200` means the connection is proven end to end.

## Stage 2 — read through the SDK (container only)

`AtriumSDK.__init__` raises `OSError` on macOS *before* it looks at the
connection type, so this cannot run on your laptop even though no local dataset
is involved. Inside the container:

```bash
export ATRIUMDB_API_TOKEN=$(python3 remote/auth0_token.py)   # optional, avoids re-minting
python3 remote/test_connection.py
```

Expected output:

```
using ATRIUMDB_API_TOKEN from the environment
token audience : https://.../api
api url        : https://.../api

connecting ...
connected - 42 measure(s) available

   id  tag                                    freq (nHz)  units
   ...
```

The script reads **measures only** — the signal types the dataset holds. It
touches no patient data and pulls no waveform blocks, so it is the cheapest
possible proof that auth, routing and the SDK's api mode all work.

### Container requirements

Not needed for stage 1, only for stage 2:

- SDK installed with the **`[remote]`** extra — `pip3 install -e "sdk[remote]"`.
  The `[cli]` extra alone omits `websockets` and `PyJWT[crypto]`.
- **`libTSC.so` built for Linux.** Still required in remote mode: waveform
  blocks arrive compressed and are decoded on your side. Metadata calls like
  `get_all_measures()` do not decode anything, but the SDK loads the library at
  construction regardless.
- Outbound **`https://`** and **`wss://`** to the API host. Metadata goes over
  REST; `get_data()` needs the websocket.

## Token lifetime

Client credentials issues **no refresh token** — when a token expires you call
Auth0 again with the same four values. Auth0 meters M2M token requests, so the
rule is *one token per process, not one per call*:

- **script run by hand** — mint each run, as `test_connection.py` does
- **while iterating** — `export ATRIUMDB_API_TOKEN=...` once per shell
- **long-running service** — mint at startup, hold in memory, re-mint near expiry

Do not write tokens to disk. Re-minting is one silent HTTP call.

Note the SDK's own `_refresh_token()` posts `grant_type=refresh_token` and will
**not** work with these credentials. It is never reached here because
`validate_token=False`; wiring up automatic re-minting is a later step.

## Troubleshooting

| symptom | likely cause |
| --- | --- |
| Auth0 `access_denied` | the client is not authorised for this audience |
| Auth0 `unauthorized_client` | client_credentials is not enabled on the application |
| Auth0 `invalid_client` | wrong client_id or client_secret |
| API `401` / `403` | token `aud` does not match what the server expects, or expired |
| API `404` on `/measures/` | wrong base path — try `/api`, `/api/v1`; or not an AtriumDB server |
| `OSError ... not currently supported on macOS` | stage 2 must run in the container |
| connects, `0 measures` | auth and routing work; the dataset is empty or not visible to this client |

Opening `$ATRIUMDB_API_URL/docs` in a browser is the fastest way to see what
the server actually exposes — FastAPI serves it by default.

## Known limits of remote mode

Once you move past measures:

- **Writes are unavailable.** Every write path raises
  `NotImplementedError("API mode is not supported for writing data")`.
- **`sdk.sql_handler` is `None`.** There is no remote SQL and no query
  passthrough — the API exposes typed endpoints only. Any code that opens a
  cursor and writes its own `SELECT` cannot run against a remote dataset.
- **Not every method has a remote path.** Roughly 49 methods branch on api
  mode; others raise. `get_data`, `get_interval_array`, `get_measure_id`,
  `get_measure_info`, `get_patient_id`, `get_patient_info` and
  `get_all_measures` all work.
