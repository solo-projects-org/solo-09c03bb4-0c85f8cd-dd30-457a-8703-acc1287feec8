"""Concurrency check: a report read must observe one committed snapshot even
when a cutoff correction commits while the read transaction is open.

Run with -I against the already running smoke server is not possible because
this uses in-process connections; it opens its own isolated DB file.
"""
import os
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db, services  # noqa: E402
from app.logic import parse_mic  # noqa: E402
from datetime import date  # noqa: E402

tmp = tempfile.mkdtemp(prefix="snapshot-")
path = os.path.join(tmp, "iso.db")
db.init_db(path)

writer = db.get_conn(path)
reader = db.get_conn(path)

# Seed one batch + cutoff (state v2).
rows = [{
    "record_no": "K1", "revision_no": 1, "patient_id": "P1",
    "sample_date": date(2026, 1, 1), "organism": "O", "drug": "D",
    "mic_raw": "8", "line_no": 2,
}]
with db.write_transaction(writer):
    db.insert_batch(writer, "seed.csv", rows)
with db.write_transaction(writer):
    db.upsert_cutoff(writer, "O", "D", 4.0, 16.0)

findings = {}


def long_report():
    # Reproduce the read-snapshot structure used by services.build_report.
    with db.read_snapshot(reader):
        findings["version_at_start"] = db.current_snapshot_version(reader)
        cutoffs_v1 = db.list_cutoffs(reader)
        time.sleep(0.8)  # writer commits a cutoff change while we are open
        cutoffs_v2 = db.list_cutoffs(reader)
        findings["version_after_wait"] = db.current_snapshot_version(reader)
        findings["same_cutoff_snapshot"] = cutoffs_v1 == cutoffs_v2
        findings["observed_r_cutoff"] = cutoffs_v1[0]["r_cutoff"]


t = threading.Thread(target=long_report)
t.start()
time.sleep(0.2)
with db.write_transaction(writer):
    db.upsert_cutoff(writer, "O", "D", 4.0, 32.0)  # commits mid-report
t.join()

# A fresh connection must see the new committed state.
fresh = db.get_conn(path)
new_version = db.current_snapshot_version(fresh)
new_cutoff = db.list_cutoffs(fresh)[0]["r_cutoff"]

print("report saw version:", findings["version_at_start"],
      "->", findings["version_after_wait"])
print("cutoff stable inside the read txn:", findings["same_cutoff_snapshot"],
      "(r_cutoff =", findings["observed_r_cutoff"], ")")
print("fresh connection sees version:", new_version, "r_cutoff =", new_cutoff)

assert findings["version_at_start"] == findings["version_after_wait"] == 2
assert findings["same_cutoff_snapshot"] is True
assert findings["observed_r_cutoff"] == 16.0
assert new_version == 3 and new_cutoff == 32.0
print("SNAPSHOT ISOLATION OK")

# Also confirm a full build_report on a pinned snapshot excludes nothing odd.
report = services.build_report(fresh, None, None)
assert report["snapshot_version"] == 3
assert report["summary"][0]["interpretable"] == 1
print("BUILD REPORT ON LATEST SNAPSHOT OK")
