#!/usr/bin/env python3
"""Checks for scripts/bq_obsolete_move.py (U3 of the BigQuery obsolete-object plan).

Hermetic: an in-memory fake stands in for the BigQuery client and records every call, so "no copy,
no delete" is asserted on the call log rather than inferred from output.

Run: uv run python scripts/test_bq_obsolete_move.py   (or from scripts/, as ci.yml does)
"""

from __future__ import annotations

import contextlib
import csv
import io
import pathlib
import sys
import tempfile
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta

HERE = pathlib.Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import bq_obsolete_move as mv
from google.api_core.exceptions import NotFound

P = "ent-kixavvik-dev"
NOW_ROOTED: list[str] = []
mv.rooted = lambda project, names: [n for n in names if n in NOW_ROOTED]
T0 = datetime(2026, 11, 1, 10, 0, 0, tzinfo=UTC)
LATER = datetime(2026, 11, 9, 8, 0, 0, tzinfo=UTC)
MUTATING = ("copy_table", "delete_table", "delete_model", "create_dataset")


@dataclass
class Obj:
    kind: str  # TABLE, VIEW or MODEL
    created: datetime = T0
    modified: datetime = T0
    num_rows: int = 10
    num_bytes: int = 100
    model_type: str = "LOGISTIC_REGRESSION"
    streaming_buffer: object = None

    @property
    def table_type(self) -> str:
        return self.kind


class Fake:
    def __init__(self, objects: dict[str, Obj], obsolete: dict[str, Obj] | None = None, dataset: bool = True):
        self.ds = {"disruptions": dict(objects), "disruptions_obsolete": dict(obsolete or {})}
        self.dataset = dataset
        self.calls: list[tuple[str, str]] = []
        self.corrupt: set[str] = set()
        self.copy_raises: set[str] = set()
        self.get_raises: set[str] = set()  # names whose lookups fail with a non-NotFound error
        self.touch_after_copy: set[str] = set()  # sources written to between copy and delete
        self.created_dataset = None

    def _split(self, ref: str) -> tuple[str, str]:
        _, d, n = ref.split(".")
        return d, n

    def get_table(self, ref):
        d, n = self._split(ref)
        if n in self.get_raises:
            raise PermissionError("403 Access Denied")
        o = self.ds[d].get(n)
        if o is None or o.kind == "MODEL":
            raise NotFound(ref)
        return o

    def get_model(self, ref):
        d, n = self._split(ref)
        o = self.ds[d].get(n)
        if o is None or o.kind != "MODEL":
            raise NotFound(ref)
        return o

    def copy_table(self, src, dst, job_config=None):
        self.calls.append(("copy_table", src))
        assert job_config.write_disposition == "WRITE_EMPTY"
        (sd, sn), (dd, dn) = self._split(src), self._split(dst)
        if sn in self.copy_raises:
            raise RuntimeError("copy job failed\nsecond line")
        assert dn not in self.ds[dd], "overwrote a destination"
        copy = replace(self.ds[sd][sn], created=LATER, modified=LATER)
        if sn in self.corrupt:
            copy.num_rows += 1
        self.ds[dd][dn] = copy
        if sn in self.touch_after_copy:
            self.ds[sd][sn] = replace(self.ds[sd][sn], modified=self.ds[sd][sn].modified + timedelta(seconds=5))
        return type("Job", (), {"result": lambda self: None})()

    def delete_table(self, ref):
        self.calls.append(("delete_table", ref))
        d, n = self._split(ref)
        del self.ds[d][n]

    def delete_model(self, ref):
        self.calls.append(("delete_model", ref))
        d, n = self._split(ref)
        del self.ds[d][n]

    def get_dataset(self, ref):
        if not self.dataset:
            raise NotFound(ref)

    def create_dataset(self, ds):
        self.calls.append(("create_dataset", ds.dataset_id))
        self.created_dataset = ds
        self.dataset = True

    def mutations(self):
        return [c for c in self.calls if c[0] in MUTATING]


def manifest(d: str, rows: list[tuple[str, str, str]], env: str = "dev") -> pathlib.Path:
    """rows: (name, type, verdict); modified is T0 for all."""
    path = pathlib.Path(d) / f"manifest-{env}.csv"
    with path.open("w", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["name", "type", "size_gb", "modified", "verdict", "reason", "evidence"])
        for name, kind, verdict in rows:
            w.writerow([name, kind, "0.001", "2026-11-01T10:00:00Z", verdict, "unreferenced", ""])
    return path


def run(fake, rows, *args):
    """main() with the fake client; returns (exit code, output)."""
    with tempfile.TemporaryDirectory() as d:
        path = manifest(d, rows)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = mv.main(["--project", P, "--manifest", str(path), *args], client=fake)
            except SystemExit as e:
                code = e.code
        return code, out.getvalue() + err.getvalue()


# ---- Moving -------------------------------------------------------------------------------------


def test_a_dry_run_changes_nothing_and_says_what_it_would_do():
    fake = Fake({"t": Obj("TABLE"), "m": Obj("MODEL")}, dataset=False)
    code, out = run(fake, [("t", "TABLE", "move"), ("m", "MODEL", "move")])
    assert code == 0 and fake.mutations() == [], fake.calls
    assert "would move  t" in out and "would move  m" in out and "would be created" in out


def test_a_table_is_copied_checked_then_deleted():
    fake = Fake({"t": Obj("TABLE")})
    code, out = run(fake, [("t", "TABLE", "move")], "--apply")
    assert code == 0 and "moved  t" in out
    assert fake.mutations() == [("copy_table", f"{P}.disruptions.t"), ("delete_table", f"{P}.disruptions.t")]
    assert "t" in fake.ds["disruptions_obsolete"] and "t" not in fake.ds["disruptions"]


def test_a_model_is_copied_checked_then_deleted_as_a_model():
    fake = Fake({"m": Obj("MODEL")})
    code, out = run(fake, [("m", "MODEL", "move")], "--apply")
    assert code == 0 and "moved  m" in out
    assert fake.mutations()[-1] == ("delete_model", f"{P}.disruptions.m")


def test_ae4_a_row_count_mismatch_leaves_the_source_and_the_run_goes_on():
    fake = Fake({"a": Obj("TABLE"), "b": Obj("TABLE")})
    fake.corrupt = {"a"}
    code, out = run(fake, [("a", "TABLE", "move"), ("b", "TABLE", "move")], "--apply")
    assert code == 1
    assert "FAILED (rows/bytes 10/100 -> 11/100)  a" in out and "moved  b" in out
    assert "a" in fake.ds["disruptions"] and ("delete_table", f"{P}.disruptions.a") not in fake.calls


def test_a_copy_that_raises_deletes_nothing():
    fake = Fake({"a": Obj("TABLE")})
    fake.copy_raises = {"a"}
    code, out = run(fake, [("a", "TABLE", "move")], "--apply")
    assert code == 1 and "FAILED (RuntimeError: copy job failed)  a" in out
    assert fake.mutations() == [("copy_table", f"{P}.disruptions.a")]


def test_ae5_a_source_changed_since_the_manifest_is_skipped():
    fake = Fake({"t": Obj("TABLE", modified=T0 + timedelta(seconds=1))})
    code, out = run(fake, [("t", "TABLE", "move")], "--apply")
    assert code == 0 and "skipped (changed-since-manifest)  t" in out and fake.mutations() == []


def test_the_modified_check_ignores_sub_second_precision():
    fake = Fake({"t": Obj("TABLE", modified=T0 + timedelta(milliseconds=900))})
    _, out = run(fake, [("t", "TABLE", "move")], "--apply")
    assert "moved  t" in out


def test_a_second_run_after_a_complete_one_does_nothing():
    fake = Fake({"t": Obj("TABLE"), "m": Obj("MODEL")})
    rows = [("t", "TABLE", "move"), ("m", "MODEL", "move")]
    run(fake, rows, "--apply")
    before = len(fake.mutations())
    code, out = run(fake, rows, "--apply")
    assert code == 0 and len(fake.mutations()) == before
    assert out.count("skipped (already-moved)") == 2


def test_a_half_finished_move_is_finished_without_overwriting():
    fake = Fake({"t": Obj("TABLE")}, obsolete={"t": Obj("TABLE", created=LATER)})
    code, out = run(fake, [("t", "TABLE", "move")], "--apply")
    assert code == 0 and "moved  t" in out
    assert fake.mutations() == [("delete_table", f"{P}.disruptions.t")]


def test_a_half_finished_move_that_does_not_match_fails():
    fake = Fake({"t": Obj("TABLE")}, obsolete={"t": Obj("TABLE", created=LATER, num_rows=3)})
    code, out = run(fake, [("t", "TABLE", "move")], "--apply")
    assert code == 1 and "FAILED (rows/bytes" in out and fake.mutations() == []


def test_a_source_recreated_after_the_move_is_not_deleted():
    fake = Fake({"m": Obj("MODEL", created=LATER, modified=T0)}, obsolete={"m": Obj("MODEL")})
    code, out = run(fake, [("m", "MODEL", "move")], "--apply")
    assert code == 0 and "skipped (recreated-since-move)  m" in out and fake.mutations() == []


# ---- Refusals -----------------------------------------------------------------------------------


def test_a_view_or_routine_row_is_refused_before_any_call():
    for kind in ("VIEW", "ROUTINE"):
        fake = Fake({"v": Obj("VIEW")})
        code, out = run(fake, [("v", kind, "move"), ("t", "TABLE", "move")])
        assert code == 2 and "only tables and models move" in out and fake.calls == [], kind


def test_only_limits_the_run_and_rejects_a_name_that_is_not_a_move_row():
    fake = Fake({"a": Obj("TABLE"), "b": Obj("TABLE"), "k": Obj("TABLE")})
    rows = [("a", "TABLE", "move"), ("b", "TABLE", "move"), ("k", "TABLE", "keep")]
    code, out = run(fake, rows, "--only", "b", "--apply")
    assert code == 0 and "moved  b" in out and "a" in fake.ds["disruptions"]
    code, out = run(fake, rows, "--only", "k")
    assert code == 2 and "not move rows in the manifest: k" in out


def test_keep_rows_are_never_touched():
    fake = Fake({"a": Obj("TABLE"), "k": Obj("TABLE")})
    run(fake, [("a", "TABLE", "move"), ("k", "TABLE", "keep")], "--apply")
    assert "k" in fake.ds["disruptions"] and all("disruptions.k" not in ref for _, ref in fake.calls)


def test_a_review_row_refuses_apply_but_not_a_dry_run():
    rows = [("a", "TABLE", "move"), ("r", "TABLE", "review")]
    fake = Fake({"a": Obj("TABLE"), "r": Obj("TABLE")})
    code, out = run(fake, rows, "--apply")
    assert code == 2 and "review rows" in out and fake.calls == []
    code, _ = run(fake, rows)
    assert code == 0 and fake.mutations() == []


def test_a_manifest_for_another_environment_is_refused_before_any_call():
    fake = Fake({"a": Obj("TABLE")})
    with tempfile.TemporaryDirectory() as d:
        path = manifest(d, [("a", "TABLE", "move")], env="prd")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            try:
                mv.main(["--project", P, "--manifest", str(path), "--apply"], client=fake)
            except SystemExit as e:
                assert e.code == 2
            else:
                raise AssertionError("accepted the prd manifest in dev")
    assert "manifest-prd.csv is not manifest-dev.csv" in err.getvalue() and fake.calls == []


# ---- The destination dataset and restore --------------------------------------------------------


def test_a_missing_destination_is_created_in_the_eu_with_no_expiry():
    fake = Fake({"a": Obj("TABLE")}, dataset=False)
    run(fake, [("a", "TABLE", "move")], "--apply")
    ds = fake.created_dataset
    assert ds.dataset_id == "disruptions_obsolete" and ds.location == "EU"
    assert ds.default_table_expiration_ms is None and ds.default_partition_expiration_ms is None
    assert "manifest-dev.csv" in ds.description


def test_an_existing_destination_is_left_as_it_is():
    fake = Fake({"a": Obj("TABLE")})
    run(fake, [("a", "TABLE", "move")], "--apply")
    assert fake.created_dataset is None


def test_restore_puts_an_object_back_without_the_modified_check():
    fake = Fake({}, obsolete={"m": Obj("MODEL", created=LATER, modified=LATER)})
    code, out = run(fake, [("m", "MODEL", "move")], "--restore", "--only", "m", "--apply")
    assert code == 0 and "restored  m" in out, out
    assert "m" in fake.ds["disruptions"] and "m" not in fake.ds["disruptions_obsolete"]
    assert fake.mutations()[-1] == ("delete_model", f"{P}.disruptions_obsolete.m")


def test_restore_needs_only():
    code, out = run(Fake({}), [("m", "MODEL", "move")], "--restore", "--apply")
    assert code == 2 and "--restore needs --only" in out


# ---- Safety checks added after review -----------------------------------------------------------


def test_restore_keeps_the_obsolete_copy_when_the_name_exists_again():
    # Same size on both sides, so only the existence of the name can stop the delete.
    fake = Fake({"m": Obj("MODEL", created=LATER)}, obsolete={"m": Obj("MODEL")})
    code, out = run(fake, [], "--restore", "--only", "m", "--apply")
    assert code == 0 and "skipped (exists in disruptions; the disruptions_obsolete copy is kept)  m" in out, out
    assert fake.mutations() == [] and "m" in fake.ds["disruptions_obsolete"]


def test_restore_works_without_the_name_in_the_manifest_and_with_review_rows():
    fake = Fake({}, obsolete={"t": Obj("TABLE", created=LATER, modified=LATER)})
    code, out = run(fake, [("other", "TABLE", "review")], "--restore", "--only", "t", "--apply")
    assert code == 0 and "restored  t" in out, out
    assert "t" in fake.ds["disruptions"] and "t" not in fake.ds["disruptions_obsolete"]


def test_restore_of_a_name_not_in_the_obsolete_dataset_fails():
    code, out = run(Fake({}), [], "--restore", "--only", "nope", "--apply")
    assert code == 1 and "FAILED (not in disruptions_obsolete)  nope" in out, out


def test_a_rerun_of_a_finished_restore_is_skipped_not_failed():
    fake = Fake({"m": Obj("MODEL", created=LATER)})
    code, out = run(fake, [], "--restore", "--only", "m", "--apply")
    assert code == 0 and "skipped (already-restored)  m" in out and fake.mutations() == [], out


def test_a_restored_object_is_not_moved_again_on_the_old_manifest():
    fake = Fake({}, obsolete={"m": Obj("MODEL")})
    run(fake, [], "--restore", "--only", "m", "--apply")
    code, out = run(fake, [("m", "MODEL", "move")], "--apply")
    assert code == 0 and "skipped (changed-since-manifest)  m" in out, out
    assert "m" in fake.ds["disruptions"]


def test_a_table_with_a_streaming_buffer_is_refused():
    fake = Fake({"t": Obj("TABLE", streaming_buffer=object())})
    code, out = run(fake, [("t", "TABLE", "move")], "--apply")
    assert code == 1 and "FAILED (rows in the streaming buffer" in out and fake.mutations() == []


def test_a_source_written_during_the_move_is_not_deleted():
    fake = Fake({"t": Obj("TABLE")})
    fake.touch_after_copy = {"t"}
    code, out = run(fake, [("t", "TABLE", "move")], "--apply")
    assert code == 1 and "FAILED (source changed during the move" in out
    assert fake.mutations() == [("copy_table", f"{P}.disruptions.t")] and "t" in fake.ds["disruptions"]


def test_an_api_error_on_one_object_does_not_stop_the_run():
    fake = Fake({"a": Obj("TABLE"), "b": Obj("TABLE")})
    fake.get_raises = {"a"}
    code, out = run(fake, [("a", "TABLE", "move"), ("b", "TABLE", "move")], "--apply")
    assert code == 1 and "FAILED (PermissionError: 403 Access Denied)  a" in out and "moved  b" in out, out


def test_a_row_a_keep_root_names_today_is_refused_before_any_call():
    fake = Fake({"a": Obj("TABLE"), "b": Obj("TABLE")})
    NOW_ROOTED.append("b")
    try:
        code, out = run(fake, [("a", "TABLE", "move"), ("b", "TABLE", "move")], "--apply")
    finally:
        NOW_ROOTED.clear()
    assert code == 2 and "a keep root names these now; re-run the inventory: b" in out and fake.calls == []


def test_dry_runs_report_finish_and_restore():
    fake = Fake({"t": Obj("TABLE")}, obsolete={"t": Obj("TABLE", created=LATER), "r": Obj("MODEL")})
    _, out = run(fake, [("t", "TABLE", "move")])
    assert "would finish  t" in out
    _, out = run(fake, [], "--restore", "--only", "r")
    assert "would restore  r" in out and fake.mutations() == []


def _run() -> None:
    tests = sorted((name, fn) for name, fn in globals().items() if name.startswith("test_") and callable(fn))
    for name, fn in tests:
        fn()
        print(f"  ok  {name}")
    print(f"\n{len(tests)} mover checks passed.")


if __name__ == "__main__":
    _run()
