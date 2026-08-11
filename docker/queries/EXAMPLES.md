# Example queries inside the container

Everything below is meant to be pasted straight into the container shell
(`docker compose run --rm atriumdb`). The pattern is always the same — only the
SQL in the middle changes:

```bash
python -c "
import sqlite3
conn = sqlite3.connect('/data/atriumdb/meta/index.db')

rows = conn.execute('''
    <<< YOUR SQL HERE >>>
''').fetchall()
print('row count:', len(rows))
for r in rows:
    print(r)
"
```

Notes:

- The dataset is mounted **read-only**, so nothing here can modify real data.
- Because the outer quotes are `"`, bash still expands `$` and backticks inside.
  If your SQL contains a `$`, switch the outer quotes to `'` and the inner
  `'''` to `"""`.
- All time columns (`dob`, `start_time`, `end_time`, `start_time_n`,
  `end_time_n`) are **epoch nanoseconds**.
- For anything longer than a one-liner, copy `query_template.py` and edit the
  marked block instead.

---

## 0. What is in there

```bash
# list tables
python -c "
import sqlite3
conn = sqlite3.connect('/data/atriumdb/meta/index.db')
for r in conn.execute('SELECT name FROM sqlite_master WHERE type=\"table\" ORDER BY name'):
    print(r[0])
"
```

Or with the sqlite CLI, which is also installed:

```bash
sqlite3 -readonly /data/atriumdb/meta/index.db ".tables"
sqlite3 -readonly /data/atriumdb/meta/index.db ".schema measure"
sqlite3 -readonly -header -column /data/atriumdb/meta/index.db "SELECT * FROM measure LIMIT 10;"
```

Main tables and their columns:

| table | columns you'll usually want |
| --- | --- |
| `measure` | `id, tag, name, freq_nhz, period_ns, code, unit, unit_label, unit_code, source_id` |
| `device` | `id, tag, name, manufacturer, model, type, bed_id, source_id` |
| `patient` | `id, mrn, gender, dob, first_name, last_name, first_seen, last_updated, weight, height` |
| `encounter` | `id, patient_id, bed_id, start_time, end_time, visit_number, source_id, last_updated` |
| `device_patient` | `id, device_id, patient_id, start_time, end_time, source_id` |
| `device_encounter` | `id, device_id, encounter_id, start_time, end_time, source_id` |
| `interval_index` | `id, measure_id, device_id, start_time_n, end_time_n` |
| `block_index` | `id, measure_id, device_id, file_id, start_byte, num_bytes, start_time_n, end_time_n, num_values` |
| `patient_history` | `id, patient_id, field, value, units, time` |
| `label` | `id, label_set_id, device_id, measure_id, label_source_id, start_time_n, end_time_n` |
| `label_set` | `id, name, parent_id` |
| `bed` / `unit` / `institution` | `id, name` (+ `unit_id` / `institution_id`) |

---

## 1. Find a measure

```bash
python -c "
import sqlite3
conn = sqlite3.connect('/data/atriumdb/meta/index.db')

rows = conn.execute('''
    SELECT *
    FROM measure
    WHERE tag = 'MDC_ECG_CARD_BEAT_RATE' AND freq_nhz = 976562500 AND unit = 'nHz'
''').fetchall()
print('row count:', len(rows))
for r in rows:
    print(r)
"
```

All distinct measure tags with how many frequency/unit variants each has:

```bash
python -c "
import sqlite3
conn = sqlite3.connect('/data/atriumdb/meta/index.db')

rows = conn.execute('''
    SELECT tag, COUNT(*) AS variants, GROUP_CONCAT(DISTINCT freq_nhz), GROUP_CONCAT(DISTINCT unit)
    FROM measure
    GROUP BY tag
    ORDER BY variants DESC, tag
''').fetchall()
print('row count:', len(rows))
for r in rows:
    print(r)
"
```

## 2. Devices, beds, units

```bash
python -c "
import sqlite3
conn = sqlite3.connect('/data/atriumdb/meta/index.db')

rows = conn.execute('''
    SELECT d.id, d.tag, d.name, d.type, b.name AS bed, u.name AS unit
    FROM device d
    LEFT JOIN bed b ON d.bed_id = b.id
    LEFT JOIN unit u ON b.unit_id = u.id
    ORDER BY u.name, b.name, d.tag
''').fetchall()
print('row count:', len(rows))
for r in rows:
    print(r)
"
```

## 3. Patient cohort — gender + age at encounter start

```bash
python -c "
import sqlite3
conn = sqlite3.connect('/data/atriumdb/meta/index.db')

# Age bounds in epoch nanoseconds (adjust years as needed)
ONE_YEAR_NS = 365 * 24 * 3600 * 1_000_000_000
ONE_MONTH_NS: int = 30 * 24 * 3600 * 1_000_000_000
age_min_ns = 2 * ONE_YEAR_NS + 5 * ONE_MONTH_NS
age_max_ns = 7 * ONE_YEAR_NS + 11 * ONE_MONTH_NS

rows = conn.execute('''
    SELECT p.*, e.start_time
    FROM encounter e
    JOIN patient p ON e.patient_id = p.id
    WHERE e.start_time >= 1577836800000000000
      AND e.start_time <= 1609459200000000000
      AND p.gender = ?
      AND p.dob IS NOT NULL
      AND (e.start_time - p.dob) >= ?
      AND (e.start_time - p.dob) <= ?
''', ('F', age_min_ns, age_max_ns)).fetchall()
print('row count:', len(rows))
for r in rows:
    print(r)
"
```

Same thing but with readable dates instead of hand-computed epoch nanoseconds:

```bash
python -c "
import sqlite3
from datetime import datetime, timezone
conn = sqlite3.connect('/data/atriumdb/meta/index.db')

def ns(s):
    return int(datetime.strptime(s, '%Y-%m-%d').replace(tzinfo=timezone.utc).timestamp()) * 1_000_000_000

rows = conn.execute('''
    SELECT COUNT(DISTINCT p.id) AS patients, COUNT(*) AS encounters, p.gender
    FROM encounter e
    JOIN patient p ON e.patient_id = p.id
    WHERE e.start_time BETWEEN ? AND ?
    GROUP BY p.gender
''', (ns('2020-01-01'), ns('2021-01-01'))).fetchall()
print('row count:', len(rows))
for r in rows:
    print(r)
"
```

Age distribution, bucketed by year:

```bash
python -c "
import sqlite3
conn = sqlite3.connect('/data/atriumdb/meta/index.db')
YEAR_NS = 365 * 24 * 3600 * 1_000_000_000

rows = conn.execute('''
    SELECT CAST((e.start_time - p.dob) / ? AS INTEGER) AS age_years,
           p.gender,
           COUNT(DISTINCT p.id) AS patients
    FROM encounter e
    JOIN patient p ON e.patient_id = p.id
    WHERE p.dob IS NOT NULL AND e.start_time > p.dob
    GROUP BY age_years, p.gender
    ORDER BY age_years
''', (YEAR_NS,)).fetchall()
print('row count:', len(rows))
for r in rows:
    print(r)
"
```

## 4. Where the actual signal data is — `interval_index`

Time coverage for one measure across devices:

```bash
python -c "
import sqlite3
from datetime import datetime, timezone
conn = sqlite3.connect('/data/atriumdb/meta/index.db')

rows = conn.execute('''
    SELECT i.device_id, d.tag AS device_tag,
           COUNT(*) AS n_intervals,
           MIN(i.start_time_n) AS first_ns,
           MAX(i.end_time_n) AS last_ns,
           SUM(i.end_time_n - i.start_time_n) / 3600000000000.0 AS covered_hours
    FROM interval_index i
    JOIN device d ON d.id = i.device_id
    JOIN measure m ON m.id = i.measure_id
    WHERE m.tag = 'MDC_ECG_CARD_BEAT_RATE'
    GROUP BY i.device_id
    ORDER BY covered_hours DESC
''').fetchall()
print('row count:', len(rows))
for r in rows:
    dev_id, tag, n, first_ns, last_ns, hours = r
    fmt = lambda x: datetime.fromtimestamp(x / 1e9, timezone.utc).isoformat()
    print(dev_id, tag, n, fmt(first_ns), fmt(last_ns), round(hours, 1))
"
```

Which measures a given device actually has data for:

```bash
python -c "
import sqlite3
conn = sqlite3.connect('/data/atriumdb/meta/index.db')

rows = conn.execute('''
    SELECT m.id, m.tag, m.freq_nhz, m.unit,
           COUNT(*) AS n_intervals,
           SUM(i.end_time_n - i.start_time_n) / 3600000000000.0 AS covered_hours
    FROM interval_index i
    JOIN measure m ON m.id = i.measure_id
    JOIN device d ON d.id = i.device_id
    WHERE d.tag = ?
    GROUP BY m.id
    ORDER BY covered_hours DESC
''', ('REPLACE_WITH_DEVICE_TAG',)).fetchall()
print('row count:', len(rows))
for r in rows:
    print(r)
"
```

## 5. Storage / block stats

```bash
python -c "
import sqlite3
conn = sqlite3.connect('/data/atriumdb/meta/index.db')

rows = conn.execute('''
    SELECT m.tag, m.freq_nhz,
           COUNT(*) AS blocks,
           SUM(b.num_values) AS values_stored,
           ROUND(SUM(b.num_bytes) / 1073741824.0, 3) AS gib
    FROM block_index b
    JOIN measure m ON m.id = b.measure_id
    GROUP BY b.measure_id
    ORDER BY gib DESC
    LIMIT 25
''').fetchall()
print('row count:', len(rows))
for r in rows:
    print(r)
"
```

## 6. Which patient was on which device, when

```bash
python -c "
import sqlite3
conn = sqlite3.connect('/data/atriumdb/meta/index.db')

rows = conn.execute('''
    SELECT dp.patient_id, p.mrn, p.gender, d.tag AS device_tag,
           dp.start_time, dp.end_time
    FROM device_patient dp
    JOIN device d ON d.id = dp.device_id
    JOIN patient p ON p.id = dp.patient_id
    WHERE dp.start_time <= 1609459200000000000
      AND (dp.end_time IS NULL OR dp.end_time >= 1577836800000000000)
    ORDER BY dp.start_time
    LIMIT 50
''').fetchall()
print('row count:', len(rows))
for r in rows:
    print(r)
"
```

## 7. Labels

```bash
python -c "
import sqlite3
conn = sqlite3.connect('/data/atriumdb/meta/index.db')

rows = conn.execute('''
    SELECT ls.name AS label_set, src.name AS source, COUNT(*) AS n,
           SUM(l.end_time_n - l.start_time_n) / 3600000000000.0 AS labelled_hours
    FROM label l
    JOIN label_set ls ON ls.id = l.label_set_id
    LEFT JOIN label_source src ON src.id = l.label_source_id
    GROUP BY l.label_set_id, l.label_source_id
    ORDER BY n DESC
''').fetchall()
print('row count:', len(rows))
for r in rows:
    print(r)
"
```

## 8. Dump a result to CSV (lands in `docker/out/` on the host)

```bash
python -c "
import csv, sqlite3
conn = sqlite3.connect('/data/atriumdb/meta/index.db')
conn.row_factory = sqlite3.Row

rows = conn.execute('''
    SELECT * FROM measure ORDER BY tag
''').fetchall()
print('row count:', len(rows))
with open('/workspace/out/measures.csv', 'w', newline='') as f:
    if rows:
        w = csv.DictWriter(f, fieldnames=rows[0].keys())
        w.writeheader()
        w.writerows(dict(r) for r in rows)
print('wrote /workspace/out/measures.csv')
"
```

---

## 9. Same thing through the SDK instead of raw SQL

The SDK is installed and `libTSC.so` is compiled for this image, so waveform
reads work too:

```bash
python -c "
from atriumdb import AtriumSDK
sdk = AtriumSDK(dataset_location='/data/atriumdb')

print('measures:', len(sdk.get_all_measures()))
print('devices :', len(sdk.get_all_devices()))
for mid, m in list(sdk.get_all_measures().items())[:10]:
    print(mid, m['tag'], m['freq_nhz'], m['unit'])
"
```

Pull actual signal values for one measure/device/time window:

```bash
python -c "
from atriumdb import AtriumSDK
sdk = AtriumSDK(dataset_location='/data/atriumdb')

# freq defaults to nHz units, and 'units' is the measure table's unit column
measure_id = sdk.get_measure_id(measure_tag='MDC_ECG_CARD_BEAT_RATE', freq=976562500, units='nHz')
device_id  = 1
start_ns   = 1577836800000000000
end_ns     = start_ns + 60 * 1_000_000_000  # one minute

headers, times, values = sdk.get_data(
    measure_id=measure_id, start_time_n=start_ns, end_time_n=end_ns, device_id=device_id)
print('blocks:', len(headers), 'samples:', values.size)
print(times[:5], values[:5])
"
```

And the CLI, which is installed as `atriumdb`:

```bash
atriumdb --dataset-location /data/atriumdb measure ls
atriumdb --dataset-location /data/atriumdb device ls
atriumdb --dataset-location /data/atriumdb patient ls --limit 20 --gender F
atriumdb --help
```
