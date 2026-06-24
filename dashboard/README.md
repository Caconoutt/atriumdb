# Measure Resolution (dashboard backend)

Implementation of the AtriumDB **measure-resolution translation layer** described in
`atriumdb_measure_resolution_spec.md`. It maps each known measure to its per-deployment
AtriumDB `measure_id` at init, and at request time converts a stable dashboard reference
into the `measure_id`(s) a data-retrieval function needs. Pulling and merging samples is
out of scope.

## Layout

```
dashboard/
  measure_mapping/
    __init__.py            public surface
    config.py              AtriumConfig.from_env() + connect_sdk()  (SDK is config-driven)
    models.py              SeedRow, ResolvedMeasure, InitReport
    db.py                  DashboardDB: reference SQLite schema + upserts + queries
    resolver.py            resolve_measure_id, initialize_measure_mapping,
                           resync_atriumdb_ids, resolve_for_retrieval
    seed_measures.csv      committed seed (136 measures, keyed on the triple)
  scripts/
    generate_seed.py       rebuilds seed_measures.csv from the corrected workbook
  tests/
    test_measure_mapping.py
```

## Usage

```python
from measure_mapping import (
    AtriumConfig, connect_sdk, DashboardDB,
    load_seed_rows, initialize_measure_mapping, resolve_for_retrieval,
)

# init (once per process; the only path that calls the SDK)
sdk = connect_sdk(AtriumConfig.from_env())
db = DashboardDB("dashboard.sqlite"); db.ensure_schema()
initialize_measure_mapping(sdk, db, load_seed_rows())

# request time (pure dashboard-DB lookup, no SDK)
for m in resolve_for_retrieval(db, vital_sign="systolic_bp"):
    ...  # hand m.atriumdb_measure_id (+ m.conversion_factor) to the retriever
```

Connection is read from env (`ATRIUMDB_MODE`, `ATRIUMDB_DATASET_LOCATION`,
`ATRIUMDB_METADATA_TYPE`, `ATRIUMDB_CONNECTION_PARAMS`, `ATRIUMDB_API_URL`,
`ATRIUMDB_TOKEN`). No `measure_id` is ever hardcoded.

After AtriumDB is reloaded with new ids, run `resync_atriumdb_ids(sdk, db, seed_rows)` to
refresh the cached ids from the triple without touching semantics.

## Tests

```
python -m pytest dashboard/tests -q
```

They run against a throwaway local AtriumDB dataset and cover the spec's five cases plus
resync and coverage. (Skipped automatically if `atriumdb` is not installed.)

## The seed

`seed_measures.csv` is generated from `AtriumDB_VitalSign_Mapping_corrected.xlsx`
(`python dashboard/scripts/generate_seed.py`). One row per measure across every tab,
keyed on the triple `(tag, freq_nhz, unit_code)`; no AtriumDB ids are carried.

| tab | measure_group | notes |
|---|---|---|
| Vital Sign Mapping | `core_vital_sign` | sets `vital_sign`; 3 kPa NIBP rows get `conversion_factor=7.50062` |
| Excluded - Non-systemic | `other_pressure` | ICP/CVP/PAP etc. — resolvable, just not a core vital |
| Clinical Review | `needs_review` | `review=true` |
| Waveforms (Phase 2) | `waveform` | 125 Hz pressure waveforms |

`Special Measures` is not a measure list — it documents the kPa→mmHg variants whose rows
already live in the Vital Sign tab; the generator uses it only to confirm the factor.

## Notes

These corrections were applied while implementing; none change the spec's intent:

1. **`freq_nhz` correction.** The workbook displays the numeric sampling rate rounded to
   `0.976562` Hz, but the true rate is `1000/1024` Hz = `0.9765625` Hz = exactly
   `976_562_500` nHz (confirmed against the SDK, matching the spec's test value).
   `freq_hz_to_nhz` corrects this so seeded triples actually resolve; waveforms map to
   `125_000_000_000` nHz.
2. **`unit_code` is the MDC string.** The workbook's `unit` column is the human label
   (`mmHg`) and its `unit_code` column is the MDC string (`MDC_DIM_MMHG`). The seed maps
   them correctly (`unit_label` ← label, `unit_code` ← MDC string), and for tabs lacking
   an explicit MDC column it derives it from the label.
3. **`get_measure_info` validation.** Verified `info["unit"]` is the MDC string (and the
   numeric ISO code lives under `info["unit_code"]`), so the spec's
   `info["unit"] != row.unit_code` mismatch check is correct as written.
4. **Coverage policy implemented** (`coverage_policy="needs_review"`, default): instance
   measures missing from the seed are auto-inserted as `needs_review` so everything stays
   resolvable; `"ignore"` skips reconciliation.
5. **`ResolvedMeasure` carries provenance** (`measure_id`, `vital_sign`, `measure_group`)
   in addition to the spec's fields, which the multi-source retriever needs to label
   results; the required fields are unchanged.
6. **`canonical_unit="mmHg"`** is set on all BP vitals (not only the kPa variants) so the
   downstream retriever has the normalization target for every blood-pressure source;
   heart-rate and non-core groups leave it `NULL`.
