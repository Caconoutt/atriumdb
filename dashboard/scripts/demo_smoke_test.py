"""End-to-end smoke test you can run by hand, with no live AtriumDB instance.

It builds a throwaway local dataset, inserts a few measures (deliberately leaving one
seeded triple out and adding one unseeded channel), runs init, then exercises
request-time resolution and prints what comes back.

    python dashboard/scripts/demo_smoke_test.py
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from atriumdb import AtriumSDK
from measure_mapping import (
    DashboardDB, load_seed_rows, initialize_measure_mapping,
    resolve_for_retrieval, resync_atriumdb_ids,
)

seed = load_seed_rows()
tmp = tempfile.mkdtemp()

# 1. Build a fake instance: insert every seeded triple EXCEPT the MDC_PRESS_BLD waveform,
#    plus one channel that is NOT in the seed.
sdk = AtriumSDK.create_dataset(dataset_location=str(Path(tmp) / "ds"), database_type="sqlite")
for row in seed:
    if row.tag == "MDC_PRESS_BLD" and row.measure_group == "waveform":
        continue
    sdk.insert_measure(measure_tag=row.tag, freq=row.freq_nhz, freq_units="nHz", units=row.unit_code)
sdk.insert_measure(measure_tag="BRAND_NEW_CHANNEL", freq=976_562_500, freq_units="nHz", units="MDC_DIM_MMHG")

# 2. Init the dashboard DB.
db = DashboardDB(":memory:"); db.ensure_schema()
report = initialize_measure_mapping(sdk, db, seed, coverage_policy="needs_review")
print("INIT:", report.summary())
print("  unresolved-in-instance:", [r.tag for r in report.unresolved])
print("  auto-inserted unseeded:", [k[0] for k in report.unseeded])

# 3. Request-time resolution: all systolic BP sources.
print("\nresolve_for_retrieval(vital_sign='systolic_bp'):")
for m in resolve_for_retrieval(db, vital_sign="systolic_bp"):
    print(f"  id={m.atriumdb_measure_id:<4} x{m.conversion_factor:<8} -> {m.canonical_unit or '-':<5} {m.display_name}")

# 4. The omitted waveform is requested by group but must be absent (id was NULL).
names = [m.display_name for m in resolve_for_retrieval(db, measure_group="waveform")]
print("\nwaveform group has", len(names), "resolved sources; MDC_PRESS_BLD present?",
      "MDC_PRESS_BLD" in names)

# 5. Resync after a 'reload' that reassigns ids.
sdk2 = AtriumSDK.create_dataset(dataset_location=str(Path(tmp) / "ds2"), database_type="sqlite")
sdk2.insert_measure(measure_tag="DECOY", freq=976_562_500, freq_units="nHz", units="MDC_DIM_MMHG")
new_id = sdk2.insert_measure(measure_tag="MDC_PRESS_BLD_ART_ABP_SYS", freq=976_562_500,
                             freq_units="nHz", units="MDC_DIM_MMHG")
resync_atriumdb_ids(sdk2, db, seed)
after = resolve_for_retrieval(db, vital_sign="systolic_bp")
abp = next(m for m in after if m.display_name.startswith("Systolic BP — Arterial line"))
print(f"\nafter resync, ABP systolic now points at id={abp.atriumdb_measure_id} (instance reassigned to {new_id})")
print("OK" if abp.atriumdb_measure_id == new_id else "MISMATCH")
