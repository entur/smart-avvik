# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Scan every observations/*.csv.gz for values that won't parse as the numeric
types we want, so the BigQuery schema can be set correctly before loading."""
import csv
import collections
import glob
import gzip
import sys
from pathlib import Path

HERE = Path(__file__).parent

bad_value_units = collections.Counter()
bad_value_ex = {}
bad_levelvalue = collections.Counter()
bad_levelvalue_ex = {}
bad_quality = collections.Counter()
bad_quality_ex = {}
total = 0


def is_float(s):
    if s == "":
        return True
    try:
        float(s)
        return True
    except ValueError:
        return False


def is_int(s):
    if s == "":
        return True
    try:
        int(s)
        return True
    except ValueError:
        return False


files = sorted(glob.glob(str(HERE / "observations" / "*.csv.gz")))
for i, f in enumerate(files):
    with gzip.open(f, "rt", newline="") as fh:
        for row in csv.DictReader(fh):
            total += 1
            if not is_float(row["value"]):
                bad_value_units[row["unit"]] += 1
                bad_value_ex.setdefault(row["unit"], row["value"])
            if not is_float(row["level_value"]):
                bad_levelvalue[row["element_id"]] += 1
                bad_levelvalue_ex.setdefault(row["element_id"], row["level_value"])
            if not is_int(row["quality_code"]):
                bad_quality[row["element_id"]] += 1
                bad_quality_ex.setdefault(row["element_id"], row["quality_code"])
    if (i + 1) % 200 == 0:
        print(f"  scanned {i+1}/{len(files)} files, {total:,} rows", file=sys.stderr, flush=True)

print(f"\nTOTAL ROWS: {total:,}")
print(f"\nnon-numeric VALUE by unit ({sum(bad_value_units.values()):,} rows):")
for u, c in bad_value_units.most_common():
    print(f"  {c:>9,}  unit={u!r}  e.g. {bad_value_ex[u]!r}")
print(f"\nnon-numeric LEVEL_VALUE ({sum(bad_levelvalue.values()):,} rows):")
for el, c in bad_levelvalue.most_common(20):
    print(f"  {c:>9,}  {el}  e.g. {bad_levelvalue_ex[el]!r}")
print(f"\nnon-integer QUALITY_CODE ({sum(bad_quality.values()):,} rows):")
for el, c in bad_quality.most_common(20):
    print(f"  {c:>9,}  {el}  e.g. {bad_quality_ex[el]!r}")
