#!/usr/bin/env python3
"""Checks for scripts/bq_obsolete_inventory.py (U1 of the BigQuery obsolete-object plan).

Hermetic: no GCP call. BigQuery, Logging and gcloud are fakes; the only subprocesses are `git
ls-files` and `copy_runtime_to_prd.sh --list` with a `bq` on PATH that fails.

**AE1 does not scan the live tree.** It classifies prd's object list as of 2026-11-07 against the
root tokens frozen in `fixtures/bq_obsolete_roots.json`, so a later code change cannot turn CI red
(CI gates every deploy). The live scan is the plan's V3, run once with the inventory.

Run: uv run python scripts/test_bq_obsolete_inventory.py   (or from scripts/, as ci.yml does)
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile
from datetime import UTC, datetime, timedelta

HERE = pathlib.Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import bq_obsolete_inventory as inv

ROOTS = json.loads((HERE / "fixtures" / "bq_obsolete_roots.json").read_text())
PRD = json.loads((HERE / "fixtures" / "bq_obsolete_prd_2026-11-07.json").read_text())
T0 = datetime(2026, 11, 7, tzinfo=UTC)
PRD_PROJECT, DEV_PROJECT = "ent-kixavvik-prd", "ent-kixavvik-dev"
AE1 = {
    f"{head}_platt_nosx{v}"
    for head in ("cancel", "arr_ge5", "arr_ge10", "arr_ge20", "arr_ge40", "dep_ge5", "dep_ge10", "dep_ge20", "dep_ge40")
    for v in ("", "_lead60", "_lead120", "_lead240", "_plan")
} | {"dur_init_reopen", "impact_remaining_noet", "duration_quantiles", "category_line_hour_impact", "predictions_today"}


def objs(*names: str, type_: str = "TABLE") -> list[inv.Obj]:
    return [inv.Obj(n, type_, 1.0, T0) for n in names]


def classify(objects, env="dev", sql=(), app=None, reads=(), team=lambda p: False, repo=None):
    repo = ROOTS if repo is None else repo
    project = PRD_PROJECT if env == "prd" else DEV_PROJECT
    rows = inv.classify(objects, inv.roots_for(env, repo, sql), repo["eval-only"], app or {}, reads, project, team)
    return {r["name"]: r for r in rows}


def audit_entry(name, principal, job_project, at, kind="tableDataRead", collection="tables"):
    return {
        "timestamp": at.strftime("%Y-%m-%dT%H:%M:%S.123456Z"),
        "protoPayload": {
            "resourceName": f"projects/{DEV_PROJECT}/datasets/disruptions/{collection}/{name}",
            "authenticationInfo": {"principalEmail": principal},
            "metadata": {"@type": "x", kind: {"jobName": f"projects/{job_project}/jobs/j1"}},
        },
    }


# ---- Acceptance examples ------------------------------------------------------------------------


def test_ae1_prd_moves_exactly_the_fifty_objects_prd_code_stopped_reading():
    sql = [" ".join(f"disruptions.{t}" for t in PRD["scheduled_query_tokens"])]
    rows = classify(objs(*(n for n, _ in PRD["objects"])), env="prd", sql=sql)
    assert len(rows) == 151
    moved = {n for n, r in rows.items() if r["verdict"] == "move"}
    assert moved == AE1, (sorted(moved - AE1), sorted(AE1 - moved))
    assert all(r["verdict"] == "keep" for n, r in rows.items() if n not in AE1)


def test_ae1_view_and_table_function_are_kept_as_deployed():
    rows = classify(
        objs("varsel_decision_groups", type_="VIEW") + objs("varsel_decision_groups_between", type_="ROUTINE"),
        env="prd",
    )
    assert {r["reason"] for r in rows.values()} == {"deployed"}, rows


def test_ae2_dev_keeps_retrain_inputs_prd_never_reads():
    names = ["cancel_platt_nosx", "dep_ge40_platt_nosx_plan", "training_calls", "bus_calls", "disruptions"]
    rows = classify(objs(*names))
    for n in names:
        assert rows[n]["verdict"] == "keep" and rows[n]["reason"] in ("retrain", "prd-copy"), rows[n]
    assert classify(objs("cancel_platt_nosx"), env="prd")["cancel_platt_nosx"]["verdict"] == "move"


def test_ae2_the_miss_model_refit_keeps_tables_it_names_only_through_ds():
    text = (inv.REPO / "data" / "queries" / "transfer_miss_backtest.py").read_text()
    assert "disruptions.exp_etcmp6_fair_scored" not in text  # the plain rule alone would miss it
    assert {"exp_etcmp6_fair_scored", "exp_transfer_pairs"} <= inv.name_tokens(text)
    rows = classify(objs("exp_etcmp6_fair_scored", "exp_transfer_pairs", "leadtime_scored"))
    assert {(r["verdict"], r["reason"]) for r in rows.values()} == {("keep", "retrain")}, rows


def test_ae3_an_app_read_after_since_makes_an_unrooted_object_review():
    rows = classify(objs("exp_orphan"), app={"exp_orphan": T0})
    assert (rows["exp_orphan"]["verdict"], rows["exp_orphan"]["reason"]) == ("review", "app-read")
    assert rows["exp_orphan"]["evidence"] == "app:2026-11-07"
    assert classify(objs("exp_orphan"))["exp_orphan"]["verdict"] == "move"


def test_the_app_read_window_starts_at_since_and_counts_only_the_service_account():
    seen = {}

    class Client:
        def query(self, sql, job_config):
            seen["sql"] = sql
            seen["params"] = {p.name: p.value for p in job_config.query_parameters}
            return type("J", (), {"result": lambda self: [{"name": "exp_x", "last": T0}]})()

    since = datetime(2026, 11, 7, 12, 38, tzinfo=UTC)
    assert inv.app_reads_since(Client(), DEV_PROJECT, since) == {"exp_x": T0}
    assert seen["params"] == {"since": since, "sa": inv.app_account(DEV_PROJECT), "project": DEV_PROJECT}
    assert "creation_time >= @since" in seen["sql"] and "user_email = @sa" in seen["sql"]


def test_a_model_is_listed_once_although_tables_lists_it_too():
    ms = 1_762_473_600_000  # 2025-11-07T00:00:00Z

    class Client:
        def query(self, sql):
            rows = [
                {"table_id": "t", "type": 1, "size_bytes": 2_000_000_000, "last_modified_time": ms},
                {"table_id": "v", "type": 2, "size_bytes": 0, "last_modified_time": ms},
                {"table_id": "m", "type": 4, "size_bytes": 5, "last_modified_time": ms},  # the model again
                {"table_id": "odd", "type": 9, "size_bytes": None, "last_modified_time": ms},
            ]
            return type("J", (), {"result": lambda self: rows})()

        def list_models(self, ref):
            return [type("M", (), {"model_id": "m", "modified": T0})()]

        def list_routines(self, ref):
            return [type("R", (), {"routine_id": "f", "modified": T0})()]

    got = {(o.name, o.type) for o in inv.list_objects(Client(), DEV_PROJECT)}
    assert got == {("t", "TABLE"), ("v", "VIEW"), ("m", "MODEL"), ("odd", "9"), ("f", "ROUTINE")}, got


# ---- Name tokens and wildcards ------------------------------------------------------------------


def test_a_dataset_variable_must_end_in_disruptions_to_count():
    assert inv.name_tokens('X = "proj.other_dataset"\nq = f"{X}.foo"') == set()
    assert inv.name_tokens('DS = "ent-kixavvik-dev.disruptions"\nq = f"{DS}.foo"') == {"foo"}
    assert inv.name_tokens('DS=ent-kixavvik-dev:disruptions\nbq show "$DS.bar" "${DS}.baz"') == {"bar", "baz"}
    assert inv.name_tokens("FROM `ent-kixavvik-dev.disruptions_obsolete.x`") == set()


def test_a_placeholder_only_name_keeps_nothing():
    text = 'DS = "p.disruptions"\nq = f"{DS}.{name} disruptions.{a}_{b}"'
    tokens = inv.name_tokens(text)
    assert tokens == {"{name}", "{a}_{b}"}
    assert [inv.pattern(t) for t in tokens] == [None, None]
    assert classify(objs("anything"), repo=ROOTS | {"retrain": sorted(tokens)})["anything"]["verdict"] == "move"


def test_shell_and_printf_placeholders_are_wildcards_too():
    text = 'DS=p.disruptions\nbq rm "${DS}.togvarsel_m_cand_$m" "$DS.x_${y}_z"\nq = "disruptions.togvarsel_tune_%s" % h'
    tokens = inv.name_tokens(text)
    assert tokens == {"togvarsel_m_cand_$m", "x_${y}_z", "togvarsel_tune_%s"}, tokens
    rows = classify(objs("togvarsel_m_cand_am0630", "x_1_z", "togvarsel_tune_pm", "togvarsel_other"),
                    repo=ROOTS | {"retrain": sorted(tokens)})
    kept = {n for n, r in rows.items() if r["verdict"] == "keep"}
    assert kept == {"togvarsel_m_cand_am0630", "x_1_z", "togvarsel_tune_pm"}, rows
    assert rows["togvarsel_other"]["verdict"] == "move"


def test_a_placeholder_is_a_wildcard():
    text = 'f"disruptions.togvarsel_m_{model}"'
    rows = classify(objs("togvarsel_m_am0630", "togvarsel_m_cand_x", "togvarsel_x"),
                    repo=ROOTS | {"deployed": sorted(inv.name_tokens(text))})
    assert rows["togvarsel_m_am0630"]["verdict"] == rows["togvarsel_m_cand_x"]["verdict"] == "keep"
    assert rows["togvarsel_x"]["verdict"] == "move"


# ---- Evidence and buckets -----------------------------------------------------------------------


def test_an_external_read_of_a_table_or_a_model_makes_review():
    for kind, collection in (("tableDataRead", "tables"), ("modelDataRead", "models")):
        read = inv.parse_read(audit_entry("exp_orphan", "someone@example.org", DEV_PROJECT, T0, kind, collection))
        assert read is not None, kind
        rows = classify(objs("exp_orphan"), reads=[read])
        assert (rows["exp_orphan"]["verdict"], rows["exp_orphan"]["reason"]) == ("review", "external-read"), kind
        assert rows["exp_orphan"]["evidence"] == "external:2026-11-07"


def test_the_audit_filter_reads_tables_and_models_and_leaves_out_the_app():
    f = inv.audit_filter(DEV_PROJECT, T0)
    assert "tableDataRead:*" in f and "modelDataRead:*" in f
    assert f'principalEmail!="{inv.app_account(DEV_PROJECT)}"' in f
    assert '"/datasets/disruptions/"' in f


def test_a_read_from_team_rors_projects_raises_nothing():
    read = inv.parse_read(audit_entry("exp_orphan", "x@example.org", "ent-smartavvik-dev", T0))
    rows = classify(objs("exp_orphan"), reads=[read])
    assert rows["exp_orphan"]["verdict"] == "move" and rows["exp_orphan"]["evidence"] == "handover:2026-11-07"


def test_a_team_read_raises_nothing():
    read = inv.parse_read(audit_entry("exp_orphan", "member@example.org", DEV_PROJECT, T0))
    rows = classify(objs("exp_orphan"), reads=[read], team=lambda p: p == "member@example.org")
    assert rows["exp_orphan"]["verdict"] == "move" and rows["exp_orphan"]["evidence"] == "team:2026-11-07"


def test_a_read_of_another_dataset_is_ignored():
    entry = audit_entry("x", "a@b", DEV_PROJECT, T0)
    entry["protoPayload"]["resourceName"] = f"projects/{DEV_PROJECT}/datasets/disruptions_obsolete/tables/x"
    assert inv.parse_read(entry) is None


def test_the_membership_check_is_refused_when_your_own_account_fails_it():
    for operator in ("", "me@example.org"):
        try:
            inv.team_check(operator, lambda p: False)
        except SystemExit as e:
            assert "gcloud auth login" in str(e)
        else:
            raise AssertionError(f"accepted operator {operator!r}")
    asked = []
    check = inv.team_check("me@example.org", lambda p: asked.append(p) or p == "me@example.org")
    assert check("other@example.org") is False and check("other@example.org") is False
    assert asked == ["me@example.org", "other@example.org"]  # cached


def test_main_stops_before_writing_when_the_login_has_expired():
    saved = inv.gcloud_account, inv.gcloud_is_member
    inv.gcloud_account, inv.gcloud_is_member = (lambda: "me@example.org"), (lambda p, g: False)
    try:
        with tempfile.TemporaryDirectory() as d:
            out = pathlib.Path(d) / "manifest-dev.csv"
            try:
                inv.main(["--project", DEV_PROJECT, "--since", "2026-11-01T00:00Z", "--out", str(out)])
            except SystemExit:
                pass
            else:
                raise AssertionError("main did not stop")
            assert not out.exists()
    finally:
        inv.gcloud_account, inv.gcloud_is_member = saved


def test_main_refuses_to_classify_without_scheduled_queries():
    saved = inv.gcloud_account, inv.gcloud_is_member, inv.scheduled_queries
    inv.gcloud_account, inv.gcloud_is_member = (lambda: "me@example.org"), (lambda p, g: True)
    inv.scheduled_queries = lambda project: []
    try:
        with tempfile.TemporaryDirectory() as d:
            out = pathlib.Path(d) / "manifest-dev.csv"
            try:
                inv.main(["--project", DEV_PROJECT, "--since", "2026-11-01T00:00Z", "--out", str(out)])
            except SystemExit as e:
                assert "No scheduled queries" in str(e), e
            else:
                raise AssertionError("classified without scheduled queries")
            assert not out.exists()
    finally:
        inv.gcloud_account, inv.gcloud_is_member, inv.scheduled_queries = saved


def test_since_must_be_a_day_old():
    since = (datetime.now(UTC) - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%MZ")
    try:
        with contextlib.redirect_stderr(io.StringIO()):
            inv.main(["--project", DEV_PROJECT, "--since", since])
    except SystemExit as e:
        assert e.code == 2
    else:
        raise AssertionError("accepted a two-hour window")


# ---- The manifest -------------------------------------------------------------------------------


def test_eval_only_and_unreferenced_moves_are_told_apart():
    rows = classify(objs("training_calls_pooled", "exp_never_named"))
    assert rows["training_calls_pooled"]["reason"] == "eval-only"
    assert rows["exp_never_named"]["reason"] == "unreferenced"


def test_a_keep_root_wins_over_the_eval_rule():
    assert "leadtime_scored" in ROOTS["eval-only"]
    assert classify(objs("leadtime_scored"))["leadtime_scored"]["reason"] == "retrain"


def test_the_manifest_has_the_columns_in_order_and_names_no_person():
    reads = [inv.parse_read(audit_entry("exp_orphan", "someone@example.org", DEV_PROJECT, T0))]
    rows = inv.classify(objs("exp_orphan", "training_calls"), inv.roots_for("dev", ROOTS, ["SELECT 1"]),
                        ROOTS["eval-only"], {}, reads, DEV_PROJECT, lambda p: False)
    buf = io.StringIO()
    inv.write_manifest(rows, buf)
    text = buf.getvalue()
    assert text.splitlines()[0] == "name,type,size_gb,modified,verdict,reason,evidence"
    assert "@" not in text and "SELECT" not in text
    assert ",2026-11-07T00:00:00Z," in text
    # team-ror's cleanup instructions split on commas: the first five columns never hold one.
    for line in text.splitlines()[1:]:
        assert len(line.split(",", 5)[:5]) == 5 and '"' not in ",".join(line.split(",")[:5])


# ---- The retrain list and the copy list ---------------------------------------------------------


def test_every_retrain_path_exists():
    for g in inv.RETRAIN:
        assert inv.tracked_files([g]), f"{g} matches no tracked file"


def test_every_file_retrain_md_names_is_classified():
    text = (inv.REPO / "docs" / "operations" / "RETRAIN.md").read_text()
    named = {pathlib.Path(m).name for m in re.findall(r"`([^`\s]+\.(?:sql|sh|py))`", text)}
    listed = {p.name for p in inv.tracked_files(inv.RETRAIN)} | set(inv.NOT_RETRAIN_INPUTS)
    assert named - listed == set(), f"RETRAIN.md names files the inventory does not classify: {sorted(named - listed)}"


def test_the_copy_list_comes_from_the_script_without_a_bq_call():
    with tempfile.TemporaryDirectory() as d:
        stub = pathlib.Path(d) / "bq"
        stub.write_text("#!/bin/sh\necho 'bq was called' >&2\nexit 1\n")
        stub.chmod(0o755)
        out = subprocess.run(
            ["bash", str(inv.REPO / "data" / "queries" / "copy_runtime_to_prd.sh"), "--list"],
            capture_output=True, text=True, check=False, env=os.environ | {"PATH": f"{d}:{os.environ['PATH']}"},
        )
    assert out.returncode == 0 and out.stderr == "", out.stderr
    names = out.stdout.split()
    # No exact count: an edit to the copy list must not turn CI red (the plan's fix 4).
    assert names and len(names) == len(set(names)), names
    assert "nosx_platt_weights" in names and "cancel_platt_nosx" not in names


def _run() -> None:
    tests = sorted((name, fn) for name, fn in globals().items() if name.startswith("test_") and callable(fn))
    for name, fn in tests:
        fn()
        print(f"  ok  {name}")
    print(f"\n{len(tests)} inventory checks passed.")


if __name__ == "__main__":
    _run()
