# AtriumDB Measure Resolution — Implementation Spec

**Audience:** a coding agent implementing the AtriumDB-side code in the dashboard backend.

**What this code does:** it is the *translation layer*. It (a) at initialization, builds the lookup
that maps each known measure to its AtriumDB `measure_id`, and (b) at request time, receives a
*desired-measure reference* (in the form set by the data contract) and converts it into the
identifier(s) a data-retrieval function needs — i.e. AtriumDB `measure_id`(s).

**Explicitly out of scope:** actually pulling samples (`get_data`), and merging/aligning series.
Those are separate functions that consume this layer's output. This spec stops at "here is the
`measure_id` (plus the metadata the retriever needs) for what the user asked for."

All SDK signatures below were verified against the installed `atriumdb` package. Re-confirm against
the version pinned in the target repo.

---

## 1. The invariant everything depends on

In AtriumDB a measure is uniquely identified by the **triple `(tag, freq_nhz, unit)`**, and the
integer `measure_id` is assigned **per deployment** — the same triple can have different ids in
dev / staging / prod, and ids change if the instance is reloaded.

Two hard rules follow:
1. **Never hardcode an AtriumDB `measure_id`.** The integer ids in the planning spreadsheet are
   reference data for one instance, not configuration.
2. **Resolve the id from the triple at init, cache it, and re-resolve on demand.** The triple is the
   portable key; the id is a per-deployment cache.

`unit` means AtriumDB's stored unit **MDC string** — `MDC_DIM_MMHG`, `MDC_DIM_KILO_PASCAL`,
`MDC_DIM_BEAT_PER_MIN`, etc. It is **not** the human label ("mmHg") and **not** the numeric ISO
code (e.g. `266016`). In the seed this is the `unit_code` column. The human label and numeric code
will not resolve.

---

## 2. Connection

```python
from atriumdb import AtriumSDK
```

`AtriumSDK` supports two modes; choose via config, never hardcode:
- **Local / direct metadata DB:** `AtriumSDK(dataset_location=..., metadata_connection_type='sqlite'|'mariadb', connection_params=...)`
- **Remote API:** `AtriumSDK(api_url=..., token=...)`

Read mode + params from env/config (`ATRIUMDB_MODE`, `ATRIUMDB_DATASET_LOCATION`,
`ATRIUMDB_METADATA_TYPE`, `ATRIUMDB_CONNECTION_PARAMS`, `ATRIUMDB_API_URL`, `ATRIUMDB_TOKEN`).
Open one handle per process and reuse it. The SDK is only touched at **init** (§5); request-time
resolution (§6) is a dashboard-DB lookup and does not call the SDK.

---

## 3. Data contract — dashboard DB tables this code reads/writes

The mapping lives in the **dashboard DB**, not in AtriumDB. Column names are normative; agree on
them before implementing. The dashboard team owns the migration — this code only populates and
reads these tables.

**`measure`** — one row per known measure; caches the resolved AtriumDB identity:

| column | type | notes |
|---|---|---|
| `id` | PK | dashboard-local, **stable**, deployment-independent — this is what the UI references |
| `atriumdb_measure_id` | int, unique, nullable | resolved id; `NULL` until/unless resolvable in this instance |
| `tag` | text | natural-key part |
| `freq_nhz` | bigint | natural-key part |
| `unit_code` | text | MDC unit string (the SDK `units` value); natural-key part |
| `unit_label` | text | human label for UI (`mmHg`) |
| `display_name` | text | UI display name (from seed `name`) |
| **unique** | | `(tag, freq_nhz, unit_code)` |

**`measure_mapping`** — semantics (one row per measure; FK → `measure.id`):

| column | type | notes |
|---|---|---|
| `measure_id` | FK | → `measure.id` |
| `measure_group` | text | UI bucket: `core_vital_sign` \| `other_pressure` \| `waveform` \| `needs_review` |
| `vital_sign` | text, nullable | `heart_rate` \| `systolic_bp` \| `diastolic_bp` \| `mean_bp`; `NULL` otherwise |
| `review` | bool | advisory only; never blocks resolution |
| `conversion_factor` | float, default `1.0` | factor to canonical unit (e.g. `7.50062` for kPa→mmHg). Returned by the resolver; **applied by the retrieval layer (out of scope)** |
| `canonical_unit` | text, nullable | e.g. `mmHg` |
| `source` | text | `seed` \| `admin` — re-seed guard (§7) |
| `updated_at` / `updated_by` | | audit |

Settled design decisions (do not regress):
- **Every measure is represented and resolvable.** `measure_group` only organizes the UI; it is not
  an access gate. Non-vital pressures, waveforms, and review items are all resolvable to ids.
- Only the four core vitals carry a non-null `vital_sign`.
- kPa NIBP measures are kept, with `conversion_factor = 7.50062`; the resolver passes the factor
  through so the downstream retriever can normalize.

---

## 4. The seed file

Canonical init input, generated from the corrected workbook
(`AtriumDB_VitalSign_Mapping_corrected.xlsx`), committed to the repo (CSV/YAML), keyed on the
**triple** (never on id), containing **all** measures (every tab), one row each.

Required columns: `tag`, `freq_nhz`, `unit_code` (MDC string), `unit_label`, `name` (display),
`measure_group`, `vital_sign` (or empty), `review`, `conversion_factor` (default `1.0`),
`canonical_unit`, `notes`.

Tab → group: `Vital Sign Mapping` → `core_vital_sign` (+ `vital_sign`; the three kPa rows get
`conversion_factor=7.50062`); `Excluded - Non-systemic` → `other_pressure`; `Clinical Review` →
`needs_review` (`review=true`); `Waveforms (Phase 2)` → `waveform`.

---

## 5. Initialization: build the id lookup

Run on dashboard DB init. This is the only part that calls the AtriumDB SDK.

```python
def initialize_measure_mapping(sdk, db, seed_rows) -> InitReport:
    report = InitReport()
    with db.transaction():
        for row in seed_rows:
            # resolve identity from the triple — do NOT trust any id from the seed
            atriumdb_id = resolve_measure_id(sdk, row.tag, row.freq_nhz, row.unit_code)

            if atriumdb_id is None:
                report.unresolved.append(row)        # absent in THIS instance; still store the row (id NULL)
            else:
                info = sdk.get_measure_info(atriumdb_id)
                if info["unit"] != row.unit_code:    # validate
                    report.unit_mismatch.append((row, info["unit"]))

            measure_pk = db.upsert_measure(          # natural key = (tag, freq_nhz, unit_code)
                tag=row.tag, freq_nhz=row.freq_nhz, unit_code=row.unit_code,
                unit_label=row.unit_label, display_name=row.name,
                atriumdb_measure_id=atriumdb_id,
            )
            db.upsert_mapping_if_seed_owned(         # admin edits win (§7)
                measure_id=measure_pk, measure_group=row.measure_group,
                vital_sign=row.vital_sign or None, review=row.review,
                conversion_factor=row.conversion_factor, canonical_unit=row.canonical_unit,
                source="seed",
            )
            report.processed += 1

    log.info(report.summary())  # processed / resolved / unresolved / unit_mismatch
    return report
```

Requirements: transactional; an unresolved triple must **not** crash boot (record it, store the row
with `atriumdb_measure_id = NULL`); log a categorized summary.

### Resolution helper (verified)

```python
def resolve_measure_id(sdk, tag, freq_nhz, unit_code) -> int | None:
    return sdk.get_measure_id(
        measure_tag=tag, freq=freq_nhz, units=unit_code, freq_units="nHz"
    )
```

Verified behaviour: `get_measure_id(...)` returns an `int` for an exact triple match and `None`
(does not raise) when there is no match — including when only the unit differs. Because the seed
pins `freq_nhz` + `unit_code`, exact match is correct. For diagnostics only, a tag's multiple ids
are available via `get_measure_id_list_from_tag(tag, freq=..., units=..., freq_units=...)`.

### Coverage check (optional but recommended)

Reconcile the seed with the live instance to catch drift:

```python
live = sdk.get_all_measures()  # {id: {id,tag,name,freq_nhz,period_ns,code,unit,unit_label,unit_code,source_id}}
live_keys = {(m["tag"], m["freq_nhz"], m["unit"]) for m in live.values()}
seed_keys = {(r.tag, int(r.freq_nhz), r.unit_code) for r in seed_rows}
unseeded   = live_keys - seed_keys   # in AtriumDB, missing from seed
unresolved = seed_keys - live_keys   # in seed, absent here
```

Policy for `unseeded` (pick one, document it): auto-insert under `needs_review` so it stays
resolvable (recommended, consistent with "everything is resolvable"), or ignore.

---

## 6. Request-time resolution: desired measure → `measure_id`(s)

This is the function the rest of the app calls. **Input is a stable dashboard reference; output is
the AtriumDB id(s) plus the metadata a retriever needs.** It is a dashboard-DB lookup only — the id
was cached at init, so no SDK call here. The client never sends an AtriumDB `measure_id` (it is
per-deployment and untrusted); it sends one of the stable references below.

Recommended data contract — accept either form:

| selector form | meaning | returns |
|---|---|---|
| `measure.id` (dashboard-local int) | one specific measure | a single resolved entry |
| `vital_sign` key (e.g. `"systolic_bp"`) and/or `measure_group` | "all sources for this vital sign/group" | a list of resolved entries |

```python
@dataclass
class ResolvedMeasure:
    atriumdb_measure_id: int
    conversion_factor: float      # 1.0 unless a unit variant; APPLIED downstream, not here
    canonical_unit: str | None
    unit_code: str
    display_name: str

def resolve_for_retrieval(db, *, measure_id=None, vital_sign=None, measure_group=None
                          ) -> list[ResolvedMeasure]:
    rows = db.query_measures(measure_id=measure_id, vital_sign=vital_sign,
                             measure_group=measure_group)
    resolved = [
        ResolvedMeasure(
            atriumdb_measure_id=r.atriumdb_measure_id,
            conversion_factor=r.conversion_factor,
            canonical_unit=r.canonical_unit,
            unit_code=r.unit_code,
            display_name=r.display_name,
        )
        for r in rows if r.atriumdb_measure_id is not None   # skip unresolved-in-this-instance
    ]
    return resolved
```

The caller hands `atriumdb_measure_id` (and, if it normalizes units, `conversion_factor`) to the
data-retrieval function. Producing the id here is the boundary of this spec.

Notes:
- A `vital_sign` selector legitimately returns **multiple** ids (e.g. systolic BP has arterial,
  NIBP, femoral, brachial sources). Returning a list is intended, not an error.
- If a selected measure has `atriumdb_measure_id = NULL` (not present in this instance), it is
  omitted from the result; the caller sees an empty list / fewer ids and can report "no source
  available" rather than failing.

---

## 7. Idempotency and re-seed

- Upsert on natural keys: `measure` on `(tag, freq_nhz, unit_code)`, `measure_mapping` on `measure_id`.
- **Admin edits win:** re-seed writes a mapping row only when the existing `source = 'seed'` (or the
  row is new); rows with `source = 'admin'` are left untouched.
- Re-running after AtriumDB was reloaded with new ids must work: the triple still resolves and
  `measure.atriumdb_measure_id` is refreshed. Provide an explicit **`resync_atriumdb_ids()`** admin
  action that re-runs only the resolution step (§5 helper) and refreshes the cached ids without
  touching semantics.

---

## 8. Edge cases

- **Multi-id tag:** avoided by pinning `freq_nhz` + `unit_code`; exact `get_measure_id`.
- **Seed triple absent in instance:** row stored with `atriumdb_measure_id = NULL`; counted
  `unresolved`; omitted from resolver output.
- **Instance measure not in seed (`unseeded`):** auto-insert as `needs_review` (recommended) or ignore.
- **Unit drift** (instance `unit` ≠ seed `unit_code`): log `unit_mismatch`; do not silently overwrite.
- **AtriumDB reloaded / new ids:** re-resolve via triple (`resync_atriumdb_ids()`).
- **Frequency units:** seed stores `freq_nhz`; always pass `freq_units="nHz"`. Never mix Hz and nHz.

---

## 9. Testing

Use a throwaway local dataset — no live instance needed:

```python
sdk = AtriumSDK.create_dataset(dataset_location=tmp, database_type="sqlite")
sdk.insert_measure(measure_tag="MDC_PRESS_BLD_ART_ABP_SYS", freq=976562500,
                   freq_units="nHz", units="MDC_DIM_MMHG")
```

Cover:
1. **Resolution:** seeded triple resolves to the inserted id; wrong-unit triple resolves to `None` (both verified true).
2. **Init idempotency:** run twice → identical DB state.
3. **Admin guard:** a row marked `source='admin'` is unchanged after re-seed.
4. **Unresolved path:** seed a triple not inserted → row exists with `atriumdb_measure_id NULL`, counted unresolved, boot succeeds.
5. **Request resolution:** `resolve_for_retrieval(vital_sign="systolic_bp")` returns the expected set of ids and omits NULL/unresolved ones.

---

## 10. Verified SDK reference (resolution-relevant)

```text
AtriumSDK(dataset_location=None, metadata_connection_type=None, connection_params=None,
          api_url=None, token=None, num_threads=1, ...)
AtriumSDK.create_dataset(dataset_location, database_type=None, connection_params=None, ...)  # tests/local

get_measure_id(measure_tag, freq=None, units=None, freq_units='nHz',
               period=None, time_units='ns') -> int | None
get_measure_id_list_from_tag(measure_tag, approx=True, freq=None, units=None, freq_units=None, ...) -> list[int]
get_measure_info(measure_id) -> dict   # keys: id, tag, name, freq_nhz, period_ns, code, unit, unit_label, unit_code, source_id
get_all_measures() -> {measure_id: {<same keys as get_measure_info>}}
```

`units` takes the MDC string (e.g. `"MDC_DIM_MMHG"`), which in the source CSV is the `unit` field —
not the human label and not the numeric code. Confirm signatures against the repo's pinned version.

---

## 11. Definition of done

- On init, `measure` + `measure_mapping` hold one row per seed measure, with `atriumdb_measure_id`
  resolved from the live instance (or `NULL` + flagged when absent).
- No AtriumDB `measure_id` is hardcoded; all come from runtime resolution of the triple.
- `resolve_for_retrieval(...)` turns a stable dashboard reference (a `measure.id`, or a
  `vital_sign`/`measure_group` key) into the AtriumDB `measure_id`(s) plus `conversion_factor` /
  `canonical_unit`, ready to hand to a data-retrieval function.
- Every measure is resolvable; grouping never gates resolution.
- Re-running init is idempotent and never clobbers admin edits; `resync_atriumdb_ids()` refreshes
  cached ids after an AtriumDB reload.
```
