# S4 — UAT connection: splitting the dashboard onto two SDK instances

**Status:** proposal, for review. No code changed.

**Branches:** written against `uat_connection`. The Auth0 material referenced
here (`remote/auth0_token.py`, `remote/REMOTE_ACCESS.md`, `remote/.env.example`)
lives only on `auth0_connection` and is **not** on this branch — porting it is
part of the work, see §6.

**Confirmed inputs (2026-09-11, extended 2026-09-12):**

* The MariaDB and the UAT API server sit on the **same metadata store**. The UAT
  dataset may be a **subset** of what MariaDB holds.
* The MariaDB account will be **read-only**.
* One SDK per endpoint — the statistics and time-series endpoints run entirely
  on the API-mode SDK, metadata lookups included (§1).
* The UAT API's behaviour is confirmed on all three points this plan depended on:
  `sdk/blocks` accepts `patient_id` as a query filter; `/intervals` clips to the
  requested window; and `GET /patients/{id}` returns `gender` and `dob` under
  exactly those names.

---

## 1. The seam: metadata from MariaDB, signal from the API

Today every dashboard router takes its SDK from one provider,
`api/dependencies.py::get_sdk_instance`, which opens a local SQLite dataset from
`ATRIUMDB_DATASET_LOCATION`. For UAT that becomes two instances:

* **`meta_sdk`** — `metadata_connection_type="mariadb"`, read-only account.
  Owns everything that is a database question: encounters, units, patients,
  the demographic cohort filter, measure definitions, block-index sums.
* **`data_sdk`** — `metadata_connection_type="api"`, Auth0 M2M token.
  Owns everything that is a signal question: waveform blocks and interval
  coverage.

Because both read the same metadata store, **`patient_id` and `measure_id` mean
the same thing on both sides**, and the two can be mixed freely within a single
request. That is what makes a call like `get_patient_info` answerable from either
side, and so makes the choice in §1 a free one rather than a forced one.

> **A note on the word "demographic",** which this codebase uses for two
> unrelated things:
>
> * the **demographic cohort filter** (Priority 1B) — send sex / age / location
>   criteria to `POST /cohorts`, get MRNs back. It runs on the raw encounter
>   join and `select_all_patients_in_list`, so it is `meta_sdk` and could never
>   be anything else.
> * **`fetch_demographics`** (`pipeline.py:275`) — for one already-resolved
>   patient, look up sex and age-at-admission to decorate a results-table row.
>   It is a thin wrapper around `get_patient_info` and follows whichever SDK its
>   endpoint uses.
>
> Only the second is affected by anything in this document.

Per endpoint:

| Endpoint | SDK | Everything it calls |
| --- | --- | --- |
| `GET /measures/hours` | `meta_sdk` | `SUM(block_index.num_values)` joined to `measure` |
| `POST /cohorts` | `meta_sdk` | encounter→bed→unit join, `select_unit`, `select_all_patients_in_list`, `get_mrn_to_patient_id_map` |
| `POST /cohorts/statistics` | `data_sdk` | `get_measure_id`, `get_mrn_to_patient_id_map`, `get_patient_info`, `get_interval_array`, `get_data` |
| `POST /cohorts/timeseries` | `data_sdk` | `get_measure_id`, `get_mrn_to_patient_id_map`, `get_patient_info`, `get_measure_info`, `get_data` |

**Decided (2026-09-12): one SDK per endpoint. No endpoint uses both.** An
earlier draft of this document routed the metadata half of the statistics and
time-series endpoints through `meta_sdk` — `get_patient_id`, `get_patient_info`
and `get_measure_id` are all answerable from either side, since both read the
same store. That is now rejected in favour of the simpler rule.

The rule to hold onto, and the one to state in the code: **an endpoint picks one
SDK and uses it for everything.** Cohorts and measure-hours need `sql_handler`,
which is `None` in api mode, so they are `meta_sdk`. Statistics and time-series
need waveform blocks, which only the API serves, so they are `data_sdk` —
including their metadata lookups, all of which have working api paths.

### Why one-SDK-per-endpoint is worth its cost

What it buys:

* **No miswiring is possible.** A mixed endpoint has a correct assignment for
  every call and no way to check it; a single-SDK endpoint has one `Depends`,
  and a call routed to the wrong place cannot exist.
* **Resolver signatures do not change.** `compute_aggregate_statistics(sdk,
  request, request_id)` and `compute_cohort_timeseries(sdk, request, request_id)`
  keep their current shape, and no function in `pipeline.py` gains a second
  parameter. This deletes most of §2's "resolver signatures" work and most of
  §9's test churn.
* **A measure missing from the UAT subset gives a clean 422 again.**
  `resolve_measure_id` now asks the API, so the §1 subset problem below only
  applies to patients, and the extra existence check considered there is
  unnecessary.

What it costs — carried in §4c, §4d and §4e:

* `get_patient_info` raises on a 404 in api mode instead of returning `None`,
  and returns the server's JSON field names unmodified.
* Patient lookups become network round trips, roughly doubling the per-request
  traffic versus the mixed design.

Both are handled below. Neither is a blocker; they are the price of the
simplicity, and they are stated here so the trade is on the record.

### Consequence of the subset: MariaDB can promise what UAT cannot deliver

`meta_sdk` sees the full store; `data_sdk` sees a subset. So a request can
resolve cleanly and still find nothing:

With every lookup on the statistics and time-series endpoints now going to
`data_sdk`, this mostly resolves itself — those endpoints see only what UAT sees,
so a measure absent from UAT raises in `resolve_measure_id` and produces the
clean 422 it always did (`pipeline.py:82-92`).

What remains is the seam between endpoints:

* `POST /cohorts` resolves MRNs against **MariaDB**, so it can return a patient
  that `POST /cohorts/statistics` then cannot find in UAT. The patient is
  excluded as `MRN_NOT_FOUND` by the second call rather than being absent from
  the first.

The patient case is arguably correct as-is — "in the cohort, no usable data" is
a real and already-modelled outcome. The measure case is worse, because it is a
whole-request failure reported as a per-patient one.

**Decided: leave it.** A patient present in MariaDB but absent from UAT is
reported as `MRN_NOT_FOUND` by the statistics and time-series endpoints, which is
an accurate description of what those endpoints can see. No cross-check between
the two endpoints is added. Recorded here so the behaviour is not mistaken for a
bug later.

---

## 2. Change 1 — two providers in `api/dependencies.py`

`api/dependencies.py` gains two providers where it has one:

```
get_meta_sdk()   -> AtriumSDK(metadata_connection_type="mariadb",
                              connection_params={host, user, password,
                                                 database, port},
                              tsc_file_location=<unused, see gotcha>)

get_data_sdk()   -> AtriumSDK(metadata_connection_type="api",
                              api_url=ATRIUMDB_API_URL,
                              token=<minted>, validate_token=False)
```

Both stay `@lru_cache(maxsize=1)` for the same reason the current one is: they
run as a FastAPI `Depends`, so without the cache a MariaDB pool and a C library
load would happen per request.

Routers then declare what they need:

| File | Endpoint | Dependency |
| --- | --- | --- |
| `api/measures_endpoints.py` | `get_measure_total_hours` | `get_meta_sdk` |
| `api/cohort_endpoints.py` | `post_cohorts` | `get_meta_sdk` |
| `api/statistics_endpoints.py` | `post_cohort_statistics` | `get_data_sdk` |
| `api/timeseries_endpoints.py` | `post_cohort_timeseries` | `get_data_sdk` |

One `Depends` each — four one-line edits, and nothing below the endpoint changes.

`api/app.py`'s docstrings claim "all routers share one SDK provider, so a single
`app.dependency_overrides` entry covers every dashboard route" — no longer true,
needs rewording.

`deploy/server.py` (the `dependency_overrides` line after `mount_dashboard`)
overrides the **upstream** provider (`tests.mock_api.sdk_dependency.get_sdk_instance`)
so the upstream routes (`/measures/`, `/patients/…`, `/sdk/blocks`) have a
working SDK. Those are metadata routes, so point it at `get_meta_sdk`.

### Resolver signatures — unchanged

With one SDK per endpoint, this section is now almost empty, which is the point.
`compute_aggregate_statistics(sdk, request, request_id)` and
`compute_cohort_timeseries(sdk, request, request_id)` keep their signatures, and
every `pipeline.py` function keeps its single `sdk` parameter. The only change
is which provider the endpoint's `Depends` names.

`cohort_resolver.resolve_cohort` also keeps its single `sdk` and its existing
api-mode branch (`cohort_resolver.py:171`), which is a separate feature — the
dashboard proxying `/cohorts` to an upstream dashboard. Unused in this
deployment; leave it.

### Backwards compatibility

Keep `get_sdk_instance` as a module-level alias of `get_meta_sdk` so the existing
local/SQLite deployment keeps working. Because resolver signatures are unchanged,
the four test override sites need only re-key onto the right provider — see §9.

### Gotcha: the mariadb branch demands a file location it will never use

`atrium_sdk.py:227` raises `"One of dataset_location, tsc_file_location must be
specified"` for `mariadb` mode, because it builds an `AtriumFileHandler`
regardless of whether any `.tsc` file is ever read. `meta_sdk` reads no
waveforms, so pass a throwaway existing directory, with a comment — it looks
like dead configuration otherwise.

### Gotcha: do not let the constructor write to UAT

`atrium_sdk.py:250-259` runs `check_mrn_column_is_text()` at construction and,
if `auto_upgrade=True`, runs `update_measure_schema()` and `upgrade_mrn_schema()`
— **DDL against the MariaDB**. Leave `auto_upgrade` at its `False` default.

The read-only account makes this safe by construction, which is the point of it.
Note the flip side: if the UAT schema has an INTEGER `mrn` column, the
constructor raises and **there is no way to fix it from here** — that is a
conversation with whoever owns the database. Worth testing the connection early
(§10 step 2) rather than discovering it during integration.

---

## 3. What the read-only account means elsewhere

Nothing the dashboard does writes, so this costs nothing — but two constructor
behaviours are worth knowing:

* `self.settings_dict = self._get_all_settings()` runs at construction and reads
  the `settings` table. A read-only grant must include it.
* `check_mrn_column_is_text()` reads schema metadata. Same.

If the grant is per-table rather than schema-wide, the tables actually touched
are: `settings`, `measure`, `block_index`, `encounter`, `bed`, `unit`,
`patient`, `patient_history`. (`block_index` is needed by
`/measures/hours`; `patient_history` by `get_patient_info(time=...)`, which is
what `fetch_demographics` calls.)

---

## 4. Change 2 — the api-mode incompatibilities

Routing metadata to `meta_sdk` removes three of the four problems from the
original draft. What remains is one real piece of work and one one-liner.

`sdk/atriumdb/` stays byte-identical to upstream, so both fixes land in
`atriumdb_dashboard/`.

### 4a. `return_nan_filled` in api mode — **blocking, and the only substantial work**

#### What "the NaN-filled window" is

Both endpoints need the signal as a **regular grid**: one slot per sample the
measure's frequency implies over the window, with `NaN` in every slot where no
sample exists. For a 1 Hz measure over a one-hour window that is a 3600-element
array, however much or little data is actually there.

The grid is what makes *availability* computable — coverage is just
`count_non_nan / len(grid)` — and, in the time-series endpoint, what makes each
bucket a fixed slice of that array rather than a timestamp lookup. This is the
`return_nan_filled=True` form of `get_data` the resolvers call today.

#### Answering the question: no SDK modification is needed

To be explicit, because this is the thing to be sure about: **nothing in
`sdk/atriumdb/` gets edited.** The NaN-filling routine is reached through
attributes the SDK already exposes on a live instance:

| what we call | how | already public? |
| --- | --- | --- |
| `sdk.block` | plain attribute, set in `__init__` (`atrium_sdk.py:180`) | yes |
| `.decode_blocks(...)` | public method on `Block` (`block.py:442`) | yes |
| `sdk.get_measure_info(...)` | public method | yes |
| `sdk._request(...)` | underscore-private, but callable as-is | by convention only |
| `sdk._block_websocket_request(...)` | same | by convention only |

The last two are private *by naming convention*; calling them requires no change
to the SDK. That is the same trade-off `cohort_resolver._post_cohorts_remote`
(`cohort_resolver.py:88-130`) already made on purpose, and for the same reason:
keep `atriumdb/` byte-identical to upstream. Worth a comment naming the upstream
function it shadows, so an SDK upgrade has something to grep for.

#### Why the fill cannot be done afterwards in numpy

NaN-filling is not post-processing on `(times, values)`. It happens inside the C
library, in `Block.decode_blocks(..., return_nan_gap=...)`
(`block.py:567-587`), which calls `fill_nan_array_with_analog`
(`block_wrapper.py:236`). That routine needs the **block headers** and the
**raw, pre-analog-scaling** value buffer, because it applies each block's own
scale factors while scattering samples onto the grid — note it runs *before* the
`analog` conversion branch at `block.py:590`. It also derives `period_ns` from
the headers itself (`block.py:517-527`), so the caller supplies only
`start_time_n` and `end_time_n`.

None of that is reconstructible from the `(times, values)` a normal `get_data`
returns. Rebuilding the grid in numpy — which the previous draft proposed —
would mean re-deriving the scaling and rounding rules and would drift from the
direct-DB path.

Now the good part. `_get_data_api` (`atrium_sdk.py:637-664`) already ends in
exactly that call:

```python
r_times, r_values, headers = self.block.decode_blocks(
    encoded_bytes, num_bytes_list, analog=analog, time_type=time_type)
```

It simply never forwards `return_nan_gap`, `start_time_n` or `end_time_n` —
`get_data`'s api branch (`atrium_sdk.py:507-513`) drops `return_nan_filled` on
the floor. Two symptoms follow:

1. **Arity.** The direct-DB nan-filled path returns a 2-tuple `(headers, values)`
   (`atrium_sdk.py:567-569`); the api path always returns a 3-tuple. Both call
   sites unpack two — `statistics_resolver.py:348`, `timeseries_resolver.py:386`
   — so api mode raises `ValueError: too many values to unpack (expected 2)`.
2. **Semantics.** Unpacked correctly it is still the wrong thing: only the
   samples that exist, with timestamps, not a regular grid. Both endpoints derive
   *availability* from the NaN fraction of that grid
   (`statistics_resolver.py:355`, and the whole per-bucket design at
   `timeseries_resolver.py:370-385`). Without it, availability silently becomes
   1.0 everywhere and the threshold stops excluding anything.

#### The fix

A `fetch_nan_filled_window(data_sdk, measure_id, patient_id, start_ns, end_ns)`
in `pipeline.py` that reproduces `_get_data_api`'s ~10 lines with the three
missing arguments forwarded:

```python
params = {'start_time': start_ns, 'end_time': end_ns, 'measure_id': measure_id,
          'device_id': None, 'patient_id': patient_id, 'mrn': None}
block_info_list = sdk._request("GET", "sdk/blocks", params=params)

if len(block_info_list) == 0:
    # Mirror atrium_sdk.py:534-537 — decode_blocks cannot handle zero blocks.
    period_ns = sdk.get_measure_info(measure_id)["period_ns"]
    return np.full(int(round((end_ns - start_ns) / period_ns)), np.nan, dtype=np.float64)

num_bytes_list = [row["num_bytes"] for row in block_info_list]
encoded_bytes = sdk._block_websocket_request(block_info_list)

_, values = sdk.block.decode_blocks(
    encoded_bytes, num_bytes_list, analog=True, time_type=1,
    return_nan_gap=True, start_time_n=start_ns, end_time_n=end_ns)
return values
```

Because this goes through the same C routine as the direct-DB path, results are
identical rather than merely equivalent — which is the whole argument for doing
it this way.

Both resolvers then call `fetch_nan_filled_window` instead of
`sdk.get_data(..., return_nan_filled=True)`.

One thing to accept knowingly: the empty-block branch is ours to maintain,
mirroring `atrium_sdk.py:534-537`. `decode_blocks` cannot be handed zero blocks,
so the "no data at all in this window" case has to build the all-NaN grid
itself.

`get_measure_info`'s api path backfills `period_ns` when the server omits it
(`atrium_sdk.py:2050-2053`), so the empty-block branch is safe.

**Worth reporting upstream.** `return_nan_filled` being accepted and silently
ignored in api mode is a genuine SDK bug — it fails loudly on the arity here,
but a caller using the 3-tuple form would get wrong answers with no error at all.
Not worth forking for; worth a message to whoever maintains the SDK.

### 4b. `get_interval_array` returns a list, not an ndarray — one-liner

`atrium_sdk.py:5226` api branch returns raw parsed JSON; the direct-DB path ends
with `np.array(arr, dtype=np.int64)`. `statistics_resolver.py:198` does
`interval_arr[:, 1] - interval_arr[:, 0]`, which raises `TypeError` on a list of
lists.

**Fix:** `interval_arr = np.asarray(interval_arr, dtype=np.int64)` before the
existing length check at `statistics_resolver.py:195`.

**Does it change any data wrangling or communication? No.** `np.asarray` on the
`[[start, end], ...]` JSON produces exactly the `(N, 2)` int64 array the
direct-DB path already returns, so `covered_ns` and every downstream number are
computed the same way. On an ndarray it is a no-op view, so the direct-DB path is
unaffected. Keep it *after* the `len(...) == 0` guard, since `np.asarray([])` has
shape `(0,)` and would break the slicing.

#### Is the window clipped over the API? Almost certainly yes

`get_interval_array(start, end)` answers "within this window, when does data
exist?" The direct-DB path **truncates each interval to the window** before
returning it:

```python
cur_interval_start = row[3] if start is None else max(row[3], start)
cur_interval_end   = row[4] if end   is None else min(row[4], end)
```

That matters because a recording stored as one continuous stretch can extend
past the window on either side, and `statistics_resolver.py:198` counts whatever
it is handed as coverage. Unclipped, a patient with six minutes of data in a
one-hour window would report `availability = 1.10` and sail past a `0.5`
threshold instead of being excluded — a silent wrong answer, since the mean over
those six minutes looks perfectly plausible.

**But the server applies that same logic.** The api branch sends exactly the
local function's own parameters, one for one:

| sent by `atrium_sdk.py:5223-5224` | local `get_interval_array` parameter |
| --- | --- |
| `measure_id` | `measure_id` |
| `device_id` | `device_id` |
| `patient_id` | `patient_id` |
| `start_time` | `start` |
| `end_time` | `end` |
| `gap_tolerance` | `gap_tolerance_nano` |

A 1:1 mapping onto a single local call is what a thin passthrough endpoint looks
like — the server runs the SDK in direct-DB mode, calls `get_interval_array`
with these arguments, and serialises the resulting `ndarray` to JSON. That also
explains why api mode returns a list of lists in the first place. So the
clipping, the gap-tolerance merging, and the `row[3] >= row[4]` skip are all
applied server-side before we ever see the rows.

**Conclusion: not a concern.** This cannot be *proved* from this repo — the API
server's source is not here, and `tests/mock_api/` has no `/intervals` endpoint
to read — but the parameter mapping makes it the only sensible reading. Worth
one assertion during the first live connection (§10 step 2) against a patient
whose recording is known to straddle a window boundary, and then forgotten
about. No `np.clip` needed.

### 4c. Unknown MRN — use the bulk map, not the per-MRN lookup

Your requirement: an unknown MRN goes into the exclusions and the remaining MRNs
process normally. Over `data_sdk` the current code does **not** do that.

`pipeline.resolve_patient_ids` (`:105`) calls `get_patient_id` per MRN and treats
`None` as "not found". The api path is:

```python
return self._request("GET", f"/patients/mrn|{mrn}", params={'time': None})['id']
```

`_request` raises `ValueError` on any non-200 (`atrium_sdk.py:5329-5331`), so one
unknown MRN in a 200-patient cohort aborts the whole request as a 422.

**Fix: switch to `get_mrn_to_patient_id_map(mrn_list=[...])`.** The SDK's api
path for that method already does exactly the right thing
(`atrium_sdk.py:3054-3062`):

```python
try:
    result_temp = self._request("GET", f"patients/mrn|{mrn}", params={'time': None})
except ValueError:
    # MRN not found on the server; skip it to match SQL-branch behavior
    continue
```

Missing MRNs are simply absent from the returned dict — identical to the
direct-DB path, and identical to what `resolve_patient_ids` already expects. So
this is a small rewrite of one function, not a new error-handling layer, and it
has precedent: `cohort_resolver.py:260` already resolves MRNs this way for the
1A path.

**Accepted, with one log line.** `ValueError` here is not a validation error —
it is the SDK's *only* transport error. `_request` raises it after the response
comes back, for any status that is not 200 (`atrium_sdk.py:5329-5331`), so one
exception class covers 404, 500, 401 and 503 alike and the `except ValueError`
above cannot tell them apart. A transient API fault therefore skips those MRNs
and reports them as `MRN_NOT_FOUND`: the cohort comes back smaller than it
should, with a plausible explanation and no error anywhere.

**Decided (2026-09-12): live with it.** This is upstream behaviour and
`sdk/atriumdb/` stays untouched. The mitigation is a single line of *dashboard*
code — log the `MRN_NOT_FOUND` count per request — so that a suspiciously small
cohort can be diagnosed as "the API was unwell" rather than "those patients do
not exist." The exclusion records already carry the per-MRN detail.

### 4d. Demographics over the API — two things to handle

This is the per-patient results-table lookup, not the demographic cohort filter
— see the note in §1. `fetch_demographics` (`pipeline.py:275-301`) reads
`info.get("gender")` and
`info.get("dob")` from `get_patient_info`. Over `data_sdk` both assumptions need
checking:

1. **It raises instead of returning `None`.** The api path is a bare
   `self._request("GET", f"/patients/id|{patient_id}", params={'time': time})`
   (`atrium_sdk.py:2874-2876`), so a patient the server does not know 404s and
   raises. `fetch_demographics` is documented as best-effort — "a dataset that
   does not record gender or dob yields `None`" — and must keep that contract.
   A `try/except ValueError` returning `(None, None)` restores it, three lines.
2. **Field names — confirmed, nothing to do.** `GET /patients/{id}` returns
   `mrn`, `gender`, `dob` and the patient's name fields. `gender` and `dob` are
   spelled exactly as `fetch_demographics` expects, so it works over the API
   unmodified. This was the one item that could have silently emptied the
   results table; it is closed.

**One thing to keep an eye on: the response carries names.** `GET /patients/{id}`
returns `first_name`, `middle_name` and `last_name` alongside the two fields the
dashboard uses. Nothing reads or forwards them today — `fetch_demographics` takes
only `gender` and `dob`, `PatientResult` carries only `mrn`, `admission_ns`,
`mean`, `sex` and `age`, and no log statement writes the `get_patient_info` dict.
Worth keeping it that way: a debug line that dumps `info` would put patient names
into the container log, which under direct-DB it never did.

Also worth knowing: `get_patient_info(time=...)` in direct-DB mode looks up
height and weight from `patient_history` at that timestamp
(`atrium_sdk.py:2908-2918`). The api path passes `time` to the server and returns
whatever comes back. The dashboard only reads `gender` and `dob`, both
time-invariant, so nothing here depends on that difference — noted only so it is
not discovered later as a surprise.

### 4e. Latency

This is the real cost of one-SDK-per-endpoint, so it is worth stating plainly.
Everything the statistics and time-series endpoints do now crosses the UAT link.
For a cohort of *N* patients with *M* admissions each:

| call | count | weight |
| --- | --- | --- |
| `get_measure_id` | 1 | trivial |
| `get_mrn_to_patient_id_map` | `N` (the api path loops one request per MRN) | small JSON |
| `get_interval_array` | `N·M` (statistics only) | small JSON |
| `get_patient_info` | ≤ `N·M`, only for entries that survive the filters | small JSON |
| `fetch_nan_filled_window` | `N·M` | **block list + websocket transfer** |

At N=200, M=1 that is roughly 800 round trips, against roughly 400 for the mixed
design that routed metadata to MariaDB. The doubling is in the cheap calls — the
expensive one, the block transfer, is unchanged and unavoidable either way — so
the wall-clock difference will be much less than 2×, but it is not nothing on a
high-latency link.

If measurement later shows the metadata chatter dominates, the mixed design in
this document's history is the escape hatch, and the two SDKs already exist to
make it a small change. Do not pre-emptively reach for it.

**Keep the `get_interval_array` pre-filter — it matters more over the API, not
less.** An earlier draft of this document suggested dropping it once §4a lands,
on the grounds that the NaN-filled window already yields coverage as its non-NaN
fraction. That was wrong, and the code says why: the availability check at
`statistics_resolver.py:188-214` ends in a `continue`, so a below-threshold
entry never reaches `_extract_patient_mean` or `fetch_demographics` at all.

It is a cheap gate in front of an expensive call, and the split makes the gap
wider:

| call | what crosses the link |
| --- | --- |
| `get_interval_array` | one plain HTTP request, a handful of interval rows of JSON |
| `fetch_nan_filled_window` | one HTTP request for the block list **plus** a websocket transfer of encoded blocks |

Dropping the gate would mean pulling full waveform data for every entry that is
about to be excluded. Under direct-DB that was a modest waste; over the UAT link
it is the single most expensive thing the endpoint does. The pre-filter should
stay, and if anything it is now the more important half.

This also settles why S2 and S3 differ here rather than one being wrong: S3
cannot pre-filter, because per-bucket availability is only knowable from the
grid it would be trying to avoid fetching (`timeseries_resolver.py:376-385`). S2
gates per window, which `get_interval_array` can answer without moving samples.

Optional, in order of value, none required for correctness:

1. **Memoise `get_patient_info` per request.** Now worth more than it was: it is
   a network call, and repeat admissions for one patient repeat the lookup.
2. **Consider one bulk patient fetch instead of per-MRN lookups.** `get_all_patients()`
   in api mode pages through `GET /patients/` 100 at a time
   (`atrium_sdk.py:3040-3053`) and returns `{patient_id: info}` — which can serve
   both the MRN→id map *and* every demographics lookup from one paged fetch,
   replacing `N` + `N·M` round trips with `ceil(total_patients / 100)`. Whether
   that wins depends entirely on how large the UAT patient table is relative to
   the cohort: it is a clear win for a big cohort against a small dataset, and a
   clear loss for a three-patient cohort against a large one. Worth measuring
   before building.
3. **Bound cohort size at the schema layer.**

Measure first; a real UAT cohort decides whether any of this is worth doing.

---

## 5. Concurrency — the thing the split changes that nothing else does

This is not an api-mode incompatibility; it is a property of the current server
that is harmless against a local dataset and becomes a real problem the moment
requests take seconds instead of milliseconds. It is the largest item in this
plan that was not visible from the endpoint list.

### Where it starts: the endpoints are `async def` around blocking code

All four are declared `async def` (`statistics_endpoints.py:41`,
`timeseries_endpoints.py:45`, `cohort_endpoints.py:42`,
`measures_endpoints.py:37`) and then call fully synchronous resolvers. FastAPI
runs an `async def` handler **on the event loop itself**, so the whole process
stops while one request runs — including `/health`.

Against a local SQLite dataset that window is milliseconds. Against UAT it is
one HTTP round trip plus one websocket transfer *per patient per admission*. A
200-patient cohort could hold the event loop for minutes, during which the
container answers nothing at all and an orchestrator healthcheck may conclude it
is dead and restart it — mid-request.

#### Why `await` is not the answer here

The natural question is why the handler cannot simply `await` the slow work and
let the loop serve other requests meanwhile. That is exactly what `await` is
for — but only when the thing being awaited actually yields control, and nothing
in this path does.

`await` is cooperative. A coroutine gives the loop a turn only at an `await` on
a genuinely non-blocking operation. There is no such operation here:

| layer | library | blocking? |
| --- | --- | --- |
| HTTP to the API | `requests` | yes — blocks the OS thread in socket code |
| block transfer | `websockets.sync.client` (`atrium_sdk.py:66`) | yes — the **sync** client, not the asyncio one |
| MariaDB | `mariadb` C connector | yes |
| decode | `ctypes` into `libTSC.so` | yes, and holds the GIL region |

`grep -c "async def\|await " atriumdb/atrium_sdk.py` returns **0**. The SDK has
no coroutines, so `await compute_aggregate_statistics(...)` is not even
well-formed; and declaring the resolvers `async def` without changing what is
underneath would achieve nothing, because `requests.get()` blocks the thread
whether or not a coroutine is wrapped around it. The loop never gets its turn
back. That is the trap: adding `async` keywords to synchronous code makes it
*look* concurrent while leaving it exactly as serial as before.

Making it genuinely awaitable would mean replacing `requests` with an async
client and `websockets.sync` with the asyncio one — a rewrite of `atriumdb/`,
which is off the table.

#### So the work has to leave the event-loop thread

**Decided (2026-09-12): Option A — plain `def` handlers.** FastAPI does the
handoff; the dashboard writes nothing. Option B is recorded below only so the
choice is legible later.

Both hand the blocking call to a worker thread; they differ only in who writes
the handoff.

**Option A — plain `def` handler.** Drop the `async`, change nothing else.
FastAPI sees a non-coroutine handler and runs it via `run_in_threadpool` for
you.

```python
@router.post("/statistics", response_model=AggregateStatisticsResponse)
def post_cohort_statistics(...):
    return compute_aggregate_statistics(meta_sdk, data_sdk, request, x_request_id)
```

**Option B — keep `async def`, offload explicitly.**

```python
from starlette.concurrency import run_in_threadpool

@router.post("/statistics", response_model=AggregateStatisticsResponse)
async def post_cohort_statistics(...):
    return await run_in_threadpool(
        compute_aggregate_statistics, meta_sdk, data_sdk, request, x_request_id)
```

These are the same mechanism — Option A is Option B with FastAPI writing the
line. A is chosen because it is less to write and less to get wrong. B would
only earn its extra line if a handler later needed genuinely async work around
the offload, or if concurrency wanted bounding by a semaphore rather than by the
threadpool's own limit.

#### What FastAPI's scheduling does and does not cover

Option A hands off *scheduling* entirely: which thread runs a handler, how many
run at once, and queueing beyond that. None of it is the dashboard's problem.

What it cannot cover is what those threads then touch. FastAPI has no knowledge
of the two SDK objects the dashboard caches, and both carry a shared resource
that is safe only while requests are serialised:

* `meta_sdk` holds **one** MariaDB connection that *raises* on a concurrent
  borrow — reachable from `/cohorts` and `/measures/hours`;
* `data_sdk` holds **one** websocket with no lock anywhere in `atrium_sdk.py` —
  reachable from `/cohorts/statistics` and `/cohorts/timeseries`.

Note that one-SDK-per-endpoint does not reduce this: each SDK is still shared
across the endpoints that use it, and two concurrent `/cohorts/statistics`
requests contend on the same websocket just as surely as a mixed design would.

Threading them is what FastAPI does; making those two objects safe to call from
several threads is ours, and it is four lines total (a constructor flag and a
lock). The two hazards below are therefore not an argument against Option A —
they are its remaining cost, and without them Option A is strictly worse than
changing nothing.

### Hazard 1 — `meta_sdk`'s MariaDB connection is single, and refuses to share

With pooling left at its default, `MariaDBHandler` uses `SingleConnectionManager`
(`maria_handler.py:55-95`), which despite the name is not a pool: it holds **one**
connection and hands it out under a borrow flag.

```python
if self._borrowed:
    raise ValueError("The connection is already borrowed, please release it before requesting it again.")
```

It raises rather than waiting. So two dashboard requests running concurrently in
the threadpool — say a `/cohorts` and a `/measures/hours` — mean one of them
gets a `ValueError` and a 500, on a perfectly valid request, non-deterministically.

**Fix:** construct `meta_sdk` with `no_pool=True`. The SDK already exposes the
flag, and `maria_db_connection` (`maria_handler.py:151-174`) then opens and
closes a connection per query instead. That is thread-safe by construction. The
cost is connection setup per query, which is the right trade against
intermittent 500s — and the dashboard's metadata queries are few and coarse.

### Hazard 2 — `data_sdk`'s websocket is shared and completely unguarded

`_block_websocket_request` (`atrium_sdk.py:666-699`) sends a comma-delimited
block-id list on `self.websock_conn`, then reads from that same connection until
it sees `Atriumdb_Done`. There is **no lock anywhere in `atrium_sdk.py`** — a
grep for `threading.Lock` returns nothing — and the connection is deliberately
kept open for the life of the SDK object, which is process-wide and cached.

Two threads calling it at once interleave on one socket: thread A sends its ids,
thread B sends its ids, and then both loops consume from the same stream. Thread
A can return thread B's blocks.

That is the worst failure shape in this whole document: **no exception**. The
bytes decode cleanly, the means are plausible, and the numbers belong to another
patient.

**Fix:** a module-level `threading.Lock` held across
`fetch_nan_filled_window`'s send-and-receive. It serialises the waveform
transfers — which are the expensive part, so raw throughput does not improve
much — but the metadata work, the MariaDB queries, and `/health` all proceed in
parallel, which is what the `async` removal was for.

If measurement later shows the serialisation is the bottleneck, the next step is
a small pool of `data_sdk` instances, one per worker thread, rather than removing
the lock.

### Hazard 3 — no HTTP call in the SDK has a timeout

`grep -c timeout atriumdb/atrium_sdk.py` returns **0**. Every `requests` call —
`_request` at `:5326`, the auth config fetch at `:300`, the token refresh at
`:5363` — runs without one. A UAT server that accepts a connection and then
stops responding hangs that worker indefinitely; with `async def` still in place
it hangs the entire process.

Partial mitigation, in order of how much it covers:

* `_request` forwards `**kwargs` to `requests.request`, so **our own** call in
  `fetch_nan_filled_window` can simply pass `timeout=...`. That covers the block
  -list fetch.
* It does **not** cover the SDK's internal calls — `get_interval_array`,
  `get_measure_info` on `data_sdk` — which build their own `_request` invocation
  with no timeout and cannot be reached without editing `atriumdb/`.
* `socket.setdefaulttimeout(...)` at startup in `deploy/server.py` is the blunt
  instrument that does cover them, at the cost of applying to every socket in
  the process, the websocket included. Workable if the value is generous, but it
  needs testing against a long block transfer before being trusted.

Worth reporting upstream alongside the `return_nan_filled` gap in §4a — a
library that makes network calls with no timeout is a problem for any
long-running service, not just this one.

### Summary of the change

| | change | why |
| --- | --- | --- |
| all four endpoints | `async def` → `def` | keep the event loop free; `/health` keeps answering |
| `meta_sdk` | `no_pool=True` | `SingleConnectionManager` raises on concurrent borrow |
| `fetch_nan_filled_window` | module-level `threading.Lock` | the websocket is shared and unguarded |
| `fetch_nan_filled_window` | `timeout=` on its `_request` call | partial; see above |

Small in lines, and none of it is optional once the endpoints go into a
threadpool. Doing the `async` removal *without* the other three would be worse
than leaving everything as it is.

---

## 6. Change 3 — token lifecycle

`remote/auth0_token.py` on `auth0_connection` mints a token correctly but is a
**CLI script**: reads `.env`, prints to stdout, `sys.exit`s on bad config. A
long-running server needs the same logic as an importable, cached provider. Port
it to `atriumdb_dashboard/auth0.py`.

Three things make this more than a copy:

**Client credentials issues no refresh token.** The SDK's `_refresh_token()`
posts `grant_type=refresh_token`, which Auth0 rejects for an M2M client. So
`data_sdk` must be built with `validate_token=False` — which means `_request`
(`atrium_sdk.py:5318-5320`) never refreshes and just uses whatever is in
`sdk.token`. Re-minting is entirely our job.

**Re-mint on a margin, inside the dependency.** `get_data_sdk` returns a cached
SDK; before handing it over it checks the cached token's expiry and, if within
~60s, mints and assigns `sdk.token`. Auth0 meters M2M token requests, so this is
one token per process held in memory — never one per call, never written to disk.

**The websocket caches the old token.** `_websocket_connect`
(`atrium_sdk.py:5336-5339`) sets `Authorization: Bearer <token>` once, and the
SDK deliberately keeps that connection open for the life of the object. A
re-mint that only updates `sdk.token` leaves `fetch_nan_filled_window` transferring over
a socket authenticated with the expired one. `_refresh_token` handles this by
closing `self.websock_conn` first (`atrium_sdk.py:5356-5359`); our helper must do
the same — set `sdk.token`, close and `None` out `sdk.websock_conn`, let the next
call reconnect.

Also port `remote/REMOTE_ACCESS.md` and `remote/.env.example`. The
`ATRIUMDB_API_URL` vs `AUTH0_AUDIENCE` distinction and the "include the version
segment in the base URL" rule documented there are the two things that otherwise
cost a day of 404s.

---

## 7. Change 4 — configuration: where the credentials live

Two sets of secrets now exist: the Auth0 client secret and the MariaDB password.
Neither ever appears in a source file, in the git history, or in a built image.

### The rule, in one line

**The dashboard reads everything from the process environment. How those
variables get into the environment is the deployment's business, not the
dashboard's.**

That single rule is what keeps local development and the real deployment from
needing different code. Locally you fill a `.env` file; in deployment the
orchestrator injects real environment variables or mounts a secret. The Python
never knows the difference.

### Where the values come from, in precedence order

1. **Real environment variables** — always win.
2. **A `.env` file**, loaded by `python-dotenv` with `override=False` so it fills
   gaps and never clobbers something the deployment set deliberately.
3. **Defaults in `config.py`** — only for non-secret values such as
   `ATRIUMDB_METADATA_CONNECTION_TYPE=sqlite` and `AUTH0_GRANT_TYPE=client_credentials`.
   No secret ever has a default.

### Where the `.env` file goes

| context | file | notes |
| --- | --- | --- |
| minting a token by hand on macOS | `remote/.env` | the existing convention from `auth0_connection`; `remote/auth0_token.py` already reads it |
| running the server locally in the container | `sdk/.env` | the directory uvicorn runs from |
| real deployment | **no file** | variables injected by the orchestrator, or `--env-file` / compose `env_file:` pointing at a path outside the build context |

`remote/.env.example` is the committed template. It carries the variable names,
the comments explaining them, and **no real values** — copied to `.env` and
filled in locally.

### Ignore files — git is fine, Docker needs one edit

**Git: verified correct, nothing to change.** `.gitignore:149` is a bare `.env`,
and git applies bare patterns at every level. Checked against real paths with
`git check-ignore`:

| path | result |
| --- | --- |
| `.env` | ignored |
| `sdk/.env` | ignored |
| `sdk/atriumdb_dashboard/.env` | ignored |
| `remote/.env` | ignored |
| `remote/.env.example` | **not** ignored — correct, the template must stay tracked |

**Docker: one real gap.** `docker/Dockerfile.dockerignore` lists `.env` with the
comment "the container gets its environment from compose." But `.dockerignore`
patterns are **not** matched at any depth the way `.gitignore`'s are — a bare
`.env` excludes only the file at the build-context root. The image is built from
`sdk/` (`docker build -f atriumdb_dashboard/docker/Dockerfile .`), so `sdk/.env`
is excluded while `sdk/atriumdb_dashboard/.env` is **not**, and the Dockerfile's
`COPY . .` would bake it into a layer.

Proposed edit to `docker/Dockerfile.dockerignore` — replace the single `.env`
line with:

```
# Secrets and local dev config. The container gets its environment at RUN time
# (--env-file / compose env_file), never from a file baked into the image.
#
# The ** prefix is load-bearing: unlike .gitignore, a bare pattern here matches
# ONLY the build-context root, leaving sdk/atriumdb_dashboard/.env to be picked
# up by "COPY . .".
**/.env
**/.env.*
**/.env-*
# ...but the committed template carries no values and must ship.
!**/.env.example
```

`**/.env-*` covers the `.env-dev` form `.gitignore` also lists; the negation
keeps `.env.example` shippable.

#### The BuildKit caveat — noted, not acted on

A per-Dockerfile ignore file (`Dockerfile.dockerignore`) is honoured only by
**BuildKit**. The legacy builder (`DOCKER_BUILDKIT=0`) reads only a
`.dockerignore` at the context root — which for this build is `sdk/`, outside
this module. Neither engine merges the two files.

So a legacy-engine build would ignore the hardening above entirely. The fix
would be a `sdk/.dockerignore` carrying the same patterns, but **that file is
outside `atriumdb_dashboard/` and therefore out of scope for this work** — the
module owns only its own subtree. Recorded here as a known limitation for
whoever owns the build:

* BuildKit is the default in current Docker, so the exposure is narrow.
* If builds must support the legacy engine, `sdk/.dockerignore` needs the same
  patterns, and the two files then have to be kept in sync.

#### One thing the edit cannot undo

Excluding a file from future builds does not remove it from images already
built. If any image was produced while a `.env` sat inside
`sdk/atriumdb_dashboard/`, those credentials are in a layer and deleting the
file later does not remove them. Treat them as exposed and rotate, rather than
assuming the fix is retroactive.

### The variables

| Group | Variable | Notes |
| --- | --- | --- |
| metadata | `ATRIUMDB_METADATA_CONNECTION_TYPE` | `sqlite` \| `mariadb`; default `sqlite` preserves today's local behaviour |
| | `ATRIUMDB_DATASET_LOCATION` | sqlite mode; also the throwaway `tsc_file_location` in mariadb mode (§2) |
| | `ATRIUMDB_MARIA_HOST` / `_PORT` / `_USER` / `_DATABASE` | mariadb mode |
| | `ATRIUMDB_MARIA_PASSWORD` | **secret** |
| data | `ATRIUMDB_API_URL` | full base URL **including any version segment** |
| | `AUTH0_TENANT` / `_AUDIENCE` / `_GRANT_TYPE` | not secret, but environment-specific |
| | `AUTH0_CLIENT_ID` | not strictly secret; treat as such |
| | `AUTH0_CLIENT_SECRET` | **secret** |
| | `ATRIUMDB_API_TOKEN` | optional; skips minting while iterating |

### How a value reaches an SDK

```
  .env file ─┐
             ├─→ process environment ─→ config.py ─→ dependencies.py ─→ AtriumSDK(...)
  orchestrator ┘   (python-dotenv,        (names,       (get_meta_sdk,
                    override=False)        validation)   get_data_sdk)
```

* **`config.py`** is the only module that touches `os.environ`. It loads the
  `.env` if present, reads every name once, and validates — failing at **startup**
  with a message naming every missing variable, rather than 500-ing on the first
  request that happens to need one. It must name *which* variables are missing
  and never echo their values.
* **`dependencies.py`** takes the validated config and builds the two cached
  SDKs: `connection_params={host, user, password, database, port}` plus
  `no_pool=True` for `meta_sdk` (§5), and `api_url` plus a freshly minted `token`
  with `validate_token=False` for `data_sdk` (§6).
* **`auth0.py`** (§6) reads only the `AUTH0_*` values, and holds the minted token
  in memory for the process lifetime. Tokens are never written to disk.

### One SDK behaviour to route around

In api mode with `token=None`, `AtriumSDK.__init__` loads a dotenv **itself**:

```python
load_dotenv(dotenv_path="./.env", override=True)
token = os.environ['ATRIUMDB_API_TOKEN']
```

Two problems: the path is relative to the current working directory rather than
anything stable, and `override=True` means it would overwrite variables the
deployment set on purpose. Passing `token=` explicitly — which §6 does anyway,
because Auth0 M2M tokens have to be minted by us — means this branch never runs.
Worth knowing so nobody "simplifies" `get_data_sdk` by dropping the argument.

### Running it

Local, in the container:

```bash
cp remote/.env.example sdk/.env      # then fill it in; gitignored
docker run --rm -p 8000:8000 --env-file sdk/.env atriumdb-sdk
```

Deployment — the same image, no file in it:

```yaml
services:
  dashboard:
    image: atriumdb-sdk
    env_file: /etc/atriumdb/dashboard.env     # outside the build context
    # or: environment: [...] injected by the orchestrator / secret store
```

`docker/docker-run-dataset.sh` currently passes exactly one `-e`
(`ATRIUMDB_DATASET_LOCATION`). It should take `--env-file` instead of growing a
list of `-e` flags — a password on a `docker run` command line is visible in the
host's process list and shell history.

### Two other deployment notes

* `docker/Dockerfile` already installs `libmariadb-dev` and `gcc`, and installs
  `.[testing]` — confirm that extra pulls in both `mariadb` **and** the
  `[remote]` dependencies (`websockets`, `PyJWT[crypto]`, `requests`,
  `python-dotenv`). Api mode raises `ImportError` at construction without them
  (`atrium_sdk.py:265-266`).
* Credentials must never reach the logs. `config.py` validation reports missing
  *names*, never values; the MariaDB and Auth0 errors that surface on a bad
  password should be caught and re-raised without the connection string.

---

## 8. Change 5 — health check

`deploy/server.py`'s `/health` deliberately touches no SDK, which is right for a
liveness probe. With two independent backends it is worth adding a separate
`/health/ready` reporting each one's reachability independently — a `SELECT 1`
through the metadata handler, and a bare token mint (or `get_all_measures()`)
through the data SDK. Otherwise "cohorts work but statistics 500" has to be
diagnosed from the outside.

Optional and small. Listed late because nothing depends on it.

---

## 9. Testing

The four dashboard test modules override the single provider:

* `tests/atriumdb_dashboard/test_dashboard_api.py:117` and `:501`
* `tests/atriumdb_dashboard/test_dashboard_statistics_api.py:176`
* `tests/atriumdb_dashboard/test_dashboard_timeseries_api.py:181`

Each becomes an override of that endpoint's own provider — `get_meta_sdk` for
the cohort and measures tests, `get_data_sdk` for statistics and time-series.
Because resolver signatures are unchanged, that is the whole migration: one key
swapped per site, and every existing fixture that builds a single SDK mock keeps
working as-is.

Genuinely new coverage needed:

1. **`fetch_nan_filled_window` (§4a)** — the important one. Same blocks, same window:
   assert the grid from the api path is identical to the direct-DB
   `return_nan_filled=True` grid. This is where a bug would be invisible in
   production — wrong availability, plausible-looking means, no error.
2. **`get_interval_array` as a list (§4b)** — one assertion, that the
   `np.asarray` conversion leaves `covered_ns` identical to the direct-DB path.
3. **Unknown MRN over the API (§4c)** — the important behavioural one. A cohort
   containing one unresolvable MRN returns 200, excludes exactly that MRN as
   `MRN_NOT_FOUND`, and processes the rest. This is the requirement most at risk
   from the all-`data_sdk` routing, so it deserves a test that would fail if
   `resolve_patient_ids` ever reverted to the per-MRN `get_patient_id` loop.
4. **Demographics degradation (§4d)** — `get_patient_info` raising yields
   `(None, None)` and an included patient, not a 500.
5. **Routing** — each endpoint touches exactly one SDK. With `MagicMock` this is
   a few lines, and it is §1's rule made executable.
6. **Concurrency (§5)** — two requests issued at once both succeed. Worth one
   test with real threads rather than mocks, because the two hazards it guards
   (`connection is already borrowed`, and interleaved websocket reads) only
   appear when handlers genuinely overlap. A `TestClient` in two threads against
   a slow mocked SDK is enough to catch a regression on either.

Per the repo's constraint, all of this runs in Docker; the SDK will not construct
on macOS.

---

## 10. Suggested order

1. Get the read-only MariaDB account and the Auth0 credentials.
2. Port `remote/` from `auth0_connection` to `uat_connection`. Three things to
   establish here, all cheap and all much better known on day one:
   * auth end to end, with `test_connection.py`;
   * that the MariaDB connection constructs — the `check_mrn_column_is_text()`
     check in §2 either passes or becomes a conversation with the DB owner;
   * the three live checks from §12: that `/intervals` clips to the window, that
     `sdk/blocks` honours `patient_id` as a filter, and — newly on the critical
     path — that `GET /patients/id|<id>` returns `gender` and `dob` under those
     names (§4d).
3. **§4b** — the `np.asarray` one-liner. Independent of everything, correct on
   the current deployment too.
4. **§4a** — `fetch_nan_filled_window` plus its test. The bulk of the work, and also
   correct on the current deployment.
5. **§4c** — switch `resolve_patient_ids` to `get_mrn_to_patient_id_map`, and
   **§4d** — guard `fetch_demographics`. Both are small and both are correct on
   the current deployment.
6. **§2** — the two providers and four `Depends` edits. Genuinely small now that
   resolver signatures are unchanged.
7. **§5** — the concurrency changes, as one commit: `async def` → `def`,
   `no_pool=True`, the websocket lock, the timeout. They are only correct
   together, so they should land together.
8. **§7** config, then **§6** token lifecycle wired into `get_data_sdk`.
9. **§9** test migration, **§8** health check, **§4e** measurement.

Steps 3 to 5 deliberately precede step 6: all four are pure fixes that are
correct on today's single-SDK deployment, and landing them first means the api
path already works the moment the providers are split.

---

## 11. What is deliberately not proposed

* **No changes to `sdk/atriumdb/`.** Every api-mode gap is worked around in the
  dashboard. `return_nan_filled` in api mode is a real upstream bug (§4a) and
  worth reporting, not worth forking for.
* **No SDK-per-request.** Both providers stay process-wide and cached. MariaDB
  pooling and the persistent websocket both assume it.
* **No function taking two SDKs, and no endpoint holding two.** Each endpoint
  depends on exactly one provider, and every helper below it keeps its single
  `sdk` parameter.
* **No merging of the statistics and time-series endpoints.** Unchanged from the
  reasoning in `timeseries_endpoints.py`'s module docstring.
* **No re-routing of `cohort_resolver`'s api-mode branch** (`cohort_resolver.py:171`).
  It is a different feature — proxying `/cohorts` to an upstream dashboard — and
  is unused in this deployment.

---

## 12. Decisions

Everything raised in review is now settled. Recorded here so the reasoning
survives the branch.

1. **One SDK per endpoint (§1).** No endpoint uses both. Statistics and
   time-series run entirely on `data_sdk`, metadata lookups included; cohorts and
   measure-hours entirely on `meta_sdk`. Chosen over the mixed design for
   simplicity and unmiswireable wiring, at the cost of roughly double the cheap
   round trips (§4e) and two api-mode quirks to handle (§4c, §4d). Resolver
   signatures stay unchanged as a result.

2. **No SDK modification (§4a).** Hard constraint. The plan reaches the NaN-fill
   routine through `sdk.block.decode_blocks` — a public method on a public
   attribute — so `sdk/atriumdb/` stays byte-identical to upstream.

3. **Subset behaviour (§1).** Largely dissolved by decision 1 — the statistics
   and time-series endpoints now see exactly what UAT sees, so a missing measure
   raises the clean 422 it always did. What remains is the seam between
   endpoints: `/cohorts` resolves MRNs against MariaDB and can hand back a
   patient that `/cohorts/statistics` reports as `MRN_NOT_FOUND`. Accepted; no
   cross-check added.

4. **MariaDB grant (§3).** Read-only across the schema, so every metadata table
   the dashboard touches is readable. No per-table enumeration needed.

5. **Interval clipping (§4b).** Not a concern. The api-mode parameters map 1:1
   onto the local `get_interval_array` signature, so the server is a thin
   passthrough running that same function in direct-DB mode — clipping, gap
   tolerance and all. One assertion during the first live connection confirms it;
   no defensive `np.clip` in the dashboard.

6. **The `get_interval_array` pre-filter stays (§4e).** An earlier draft had this
   backwards. The availability check gates an early `continue`
   (`statistics_resolver.py:188-214`), so it is a cheap HTTP call standing in
   front of an expensive websocket transfer. Splitting the SDKs makes that gate
   *more* valuable, not less. S3's not having one is correct for S3 — per-bucket
   availability is only knowable from the grid it would be avoiding.

7. **Unknown-MRN error handling (§4c).** `resolve_patient_ids` switches to
   `get_mrn_to_patient_id_map`, whose api path already skips unresolvable MRNs
   the way the direct-DB path does. Its blanket `except ValueError` cannot
   distinguish a 404 from a server fault, but that is upstream behaviour and the
   SDK stays untouched; mitigated by logging the `MRN_NOT_FOUND` count per
   request.

8. **Concurrency model (§5).** Option A — plain `def` handlers, FastAPI does the
   threadpool handoff. The dashboard adds no scheduling code of its own. The two
   shared-resource fixes that Option A requires (`no_pool=True` on `meta_sdk`, a
   lock around the websocket in `fetch_nan_filled_window`) ship in the same
   commit, since FastAPI cannot see inside the cached SDK objects.

### Live checks — all closed (2026-09-12)

The three behaviours that could not be read from this repo have been confirmed
against the UAT API:

1. **`sdk/blocks` accepts `patient_id` as a query filter.**
   `fetch_nan_filled_window` passes it straight through, so the per-patient
   window fetch is sound.
2. **`/intervals` clips to the requested window**, matching the direct-DB path.
   No `np.clip` in the dashboard, per decision 5.
3. **`GET /patients/{id}` returns `gender` and `dob` under those names**, so
   `fetch_demographics` works over the API unmodified (§4d).

No open questions remain. What is left in §4d is the 404 guard — a patient the
server does not know raises rather than returning `None` — which is a property of
`_request`, not of the payload, and is unaffected by these confirmations.
