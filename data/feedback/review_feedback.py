import marimo

__generated_with = "0.23.16"
app = marimo.App(width="medium")


@app.cell(hide_code=True)
def _(mo):
    mo.md("""
    # Review varsel feedback

    Runs the bounded export in memory and reads it here, with the gate's synthetic
    residue filtered out. PostHog holds the isolated response; BigQuery holds the exact
    alert the traveller was looking at; `context_id` is the only join key.

    It opens on **prd** over the **last 30 days, today included**, and fetches as soon as it
    loads — there is nothing to set and nothing to press. That is PostHog's whole retention
    window, so the default view is every response that still exists. The controls below are
    the override; **Refresh** asks the same window again, which is how feedback that arrived
    since the page loaded appears.

    **This notebook must never move to `eval/`.** `disruptions/api/export_eval.sh` globs
    `eval/*.py`, renders every notebook it finds to static HTML, and that directory is
    *committed* and shipped in the image — which would put travellers' free text and their
    routes in git and on a public host. It lives in `data/feedback/` for that reason alone.

    **Nothing is written to disk** unless you ask for it in the last cell. Table downloads
    are disabled throughout: the exporter creates its output `0600` and refuses to
    overwrite, and a CSV dropped in `~/Downloads` by a table widget has neither property.

    Supply the PostHog **read-only personal API key** through the environment when you
    launch, never as an argument — the same one-command prefix form the README documents,
    for the same reason (argv is readable by every user on the machine via `ps`):

    ```sh
    POSTHOG_PERSONAL_API_KEY="$(approved-secret-manager-or-cli-command)" uv run marimo edit data/feedback/review_feedback.py
    ```

    BigQuery uses your developer ADC. Everything you read here is subject to the same rules
    as the export file: don't paste it into tickets, and don't keep it past 30 days.
    """)
    return


@app.cell
def imports():
    import importlib.util
    import json
    import os
    import re
    from datetime import UTC, date, datetime, timedelta
    from pathlib import Path

    import marimo as mo

    # The exporter is a script rather than an installed package, so it is loaded by path —
    # the same way its own test suite loads it. Loading it instead of reimplementing the four
    # calls is what keeps this page honest: the revision collapse, the identifier validation,
    # the duplicate-context resolution and the issue taxonomy are all the tool's, so a fix
    # there reaches this notebook without anybody remembering to mirror it. A divergent copy
    # would be worse than no notebook, because it would look like the export and not be it.
    _here = mo.notebook_dir() or Path.cwd()
    _spec = importlib.util.spec_from_file_location(
        "export_varsel_feedback", _here / "export_varsel_feedback.py"
    )
    exporter = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(exporter)

    # Which PostHog project serves which environment. tst deliberately shares DEV's project
    # (259080) and is separated from it by the `environment` event property alone, so the pair
    # is encoded here rather than left to whoever is typing --posthog-project-id at 16:55.
    TARGETS = {
        "dev": ("ent-smartavvik-dev", 259080),
        "tst": ("ent-smartavvik-tst", 259080),
        "prd": ("ent-smartavvik-prd", 259077),
    }
    return (
        Path,
        TARGETS,
        UTC,
        date,
        datetime,
        exporter,
        json,
        mo,
        os,
        re,
        timedelta,
    )


@app.cell(hide_code=True)
def controls(TARGETS, date, mo, timedelta):
    # Defaults are what this page is opened for: prd, the last thirty days including today.
    # The window ran two days on dev behind a "Run export" button until 2026-09-08, which meant
    # three widgets and a click before anything appeared — and the pre-click state renders as
    # `mo.stop`'s "an ancestor was stopped" on every downstream cell, which reads like a broken
    # notebook rather than like a prompt. The fetch now runs on load and on any change to these
    # three, so the controls are the override rather than the entry fee. `date.today()` is the
    # LOCAL date against a window labelled UTC; the exclusive end is today + 1, so today is
    # covered whichever of the two calendars is a few hours ahead.
    #
    # Thirty and not seven because the feedback is SPARSE — six responses in the trailing week of
    # prd on 2026-09-08 — so a week-wide default is mostly an empty table. Thirty is also the whole
    # of PostHog's 30-day event retention, which makes this the widest default that can return
    # anything: a longer one would only add days the project has already deleted, and would leave
    # no room under the exporter's own 31-day cap. Contexts are retained 45 days, so every response
    # in this window can still be joined to the alert behind it — the reason `expired_context`
    # should stay absent from `issues` at this width.
    _today = date.today()
    environment = mo.ui.dropdown(
        options=list(TARGETS), value="prd", label="Environment"
    )
    from_date = mo.ui.date(value=_today - timedelta(days=29), label="From (UTC, inclusive)")
    to_date = mo.ui.date(value=_today + timedelta(days=1), label="To (UTC, **exclusive**)")
    # Not a gate any more: the fetch re-runs on its own when the window changes, and this only
    # forces the same window to be asked again. Worth having because `fetch_posthog` sets
    # force_blocking — asking twice is how newly ingested feedback appears, not a repeat of a
    # cached answer.
    refresh = mo.ui.run_button(label="Refresh")
    mo.vstack([
        mo.hstack([environment, from_date, to_date], justify="start", gap=1),
        refresh,
    ])
    return environment, from_date, refresh, to_date


@app.cell(hide_code=True)
def target(TARGETS, environment, from_date, mo, to_date):
    project, posthog_project_id = TARGETS[environment.value]
    _days = (to_date.value - from_date.value).days
    # The tool's own bounds, checked here so a bad window costs a message rather than a
    # traceback halfway through a network call. It is RETURNED, not just rendered: nothing
    # stands between these widgets and the network any more, so a window the user is halfway
    # through editing (To moved before From, or dragged past 31 days) reaches the fetch on the
    # keystroke. A message read by a human was enough while a button separated the two.
    window_problem = (
        "`To` must be after `From` (it is exclusive)." if _days <= 0
        else "The window is limited to 31 days." if _days > 31
        else None
    )
    mo.md(
        f"**{environment.value}** → BigQuery `{project}` · PostHog project `{posthog_project_id}`"
        f" · {_days} day(s)"
        + (f"\n\n/// attention | Fix the window\n{window_problem}\n///" if window_problem else "")
    )
    return posthog_project_id, project, window_problem


@app.cell
def fetch(
    UTC,
    datetime,
    environment,
    exporter,
    from_date,
    mo,
    os,
    posthog_project_id,
    project,
    refresh,
    timedelta,
    to_date,
    window_problem,
):
    # Read so that pressing Refresh re-runs this cell. The button resets itself to False
    # without notifying, so reading it cannot make the fetch fire twice.
    refresh.value
    mo.stop(
        window_problem is not None,
        mo.md("_Fix the window above before anything is fetched._"),
    )
    _token = os.environ.get(exporter.TOKEN_ENV)
    mo.stop(
        not _token,
        mo.md(
            f"`{exporter.TOKEN_ENV}` is not set. Relaunch with the key in the environment —"
            " see the notes at the top. Do not paste it into a cell: a notebook keeps cell"
            " source in the file, and this file is in git."
        ),
    )

    # The same four calls main() makes, in the same order, minus write_json. Skipping the
    # write is not a shortcut: write_json refuses to overwrite an existing file, so a
    # notebook that wrote on every run would fail on its second one. Saving is opt-in at the
    # bottom instead, through that same guarded function.
    _events, _errors = exporter.collapse_events(
        exporter.fetch_posthog(
            project_id=posthog_project_id,
            environment=environment.value,
            start=from_date.value,
            end=to_date.value,
            token=_token,
        ),
        environment=environment.value,
        start=from_date.value,
        end=to_date.value,
    )
    _context_ids = sorted({event["context_id"] for event in _events})
    _contexts = exporter.fetch_contexts(
        project=project,
        context_ids=_context_ids,
        # One day earlier than the response window, because a response can be revised after
        # midnight against a context minted the day before.
        start=from_date.value - timedelta(days=1),
        end=to_date.value,
    )
    responses, issues = exporter.enrich(
        _events,
        _contexts,
        _errors,
        retained_since=datetime.now(UTC).date() - timedelta(days=45),
    )
    return issues, responses


@app.cell(hide_code=True)
def filter_controls(mo):
    # Editable, and deliberately not clever. The prefix is a CONVENTION in free text that the
    # gate's synthetic submissions happened to follow — nothing enforces it and no column
    # records it — so a pattern that tried to be forgiving (fuzzy spellings, substring
    # matches) would silently discard real feedback, which is the one failure this page must
    # not have. Widen it by hand when you know why, and read the dropped list below.
    pattern = mo.ui.text(
        value=r"^\s*(syntetisk|synthetic)",
        label="Treat as synthetic (case-insensitive regex on the comment)",
        full_width=True,
    )
    pattern
    return (pattern,)


@app.cell
def partition(issues, mo, pattern, re, responses):
    try:
        _synthetic = re.compile(pattern.value, re.IGNORECASE)
    except re.error as _exc:
        mo.stop(True, mo.md(f"/// attention | Bad regex\n`{_exc}`\n///"))

    def _is_synthetic(record):
        # `or ""` and not `.get(...)`: a thumbs-up with no comment stores comment = None, and
        # None has no .search target. Those responses are real and must survive the filter.
        return bool(_synthetic.search(record["feedback"].get("comment") or ""))

    kept = [record for record in responses if not _is_synthetic(record)]
    dropped = [record for record in responses if _is_synthetic(record)]

    # Issues are keyed to a response, so pruning the responses without pruning these leaves
    # entries pointing at rows that are no longer here — which reads as a real data problem
    # rather than as the filter doing its job.
    _dropped_ids = {record["feedback"]["response_id"] for record in dropped}
    kept_issues = [issue for issue in issues if issue.get("response_id") not in _dropped_ids]
    return dropped, kept, kept_issues


@app.cell(hide_code=True)
def summary(dropped, kept, kept_issues, mo, responses):
    mo.md(f"""
    **{len(kept)}** responses to review · **{len(dropped)}** filtered as synthetic"
        f" · **{len(kept_issues)}** issues · {len(responses)} exported in total
    """)
    return


@app.cell(hide_code=True)
def _(mo):
    mo.md("""
    ## Responses — select a row to open it
    """)
    return


@app.cell(hide_code=True)
def table(kept, mo):
    def _row(index, record):
        feedback, context = record["feedback"], record["context"] or {}
        comment = feedback.get("comment") or ""
        return {
            "#": index,
            "submitted": str(feedback.get("submitted_at") or "")[:19],
            "rating": "up" if feedback.get("rating") == "up" else "down",
            "operator": context.get("operator") or "-",
            "line": (context.get("line") or "-").replace("VYG:Line:", ""),
            "shown": context.get("display_source") or "NO CONTEXT",
            "status": context.get("message_status") or "-",
            "comment": comment[:60] + ("…" if len(comment) > 60 else ""),
        }

    selector = mo.ui.table(
        [_row(index, record) for index, record in enumerate(kept)],
        selection="single",
        # This page renders free text beside a route. The exporter creates its own output
        # 0600 and refuses to overwrite or follow a symlink; a browser download button has
        # none of that, and lands the same content in ~/Downloads for as long as it sits
        # there. The save cell at the bottom is the way out of this notebook.
        show_download=False,
        page_size=25,
        label="",
    )
    selector if kept else mo.md("_Nothing to show for this window and filter._")
    return (selector,)


@app.cell(hide_code=True)
def detail(json, kept, mo, selector):
    mo.stop(not selector.value, mo.md("_Select a row above to see the full context._"))

    _record = kept[selector.value[0]["#"]]
    _feedback = _record["feedback"]
    _context = _record["context"]

    def _blob(value, label):
        if not value:
            return mo.md(f"_{label}: none_")
        return mo.accordion({label: mo.json(json.dumps(value, default=str, ensure_ascii=False))})

    _verdict = "👍 useful" if _feedback.get("rating") == "up" else "👎 not useful"
    _head = mo.md(f"""
    ### {_verdict} — {_feedback.get("submitted_at")}

    > {_feedback.get("comment") or "_(no comment)_"}

    `response_id` {_feedback.get("response_id")} · revision {_feedback.get("revision")}
    · `context_id` {_feedback.get("context_id")}
    """)

    if _context is None:
        # Not a rendering edge case — it is the finding. The response cannot be reviewed,
        # and `issues` below says whether the context expired or never arrived.
        _body = mo.md(
            "/// attention | No context\nThe alert snapshot behind this response is not in"
            " BigQuery, so there is no way to know what the traveller was looking at. See"
            " **Issues** below for which of the two reasons applies.\n///"
        )
    else:
        _journey = _context.get("journey_context") or {}
        _body = mo.vstack([
            mo.md(f"""
            **What the traveller saw**

            > {_context.get("displayed_text") or "_(nothing recorded)_"}

            {_context.get("detail_text") or ""}

            {_context.get("observation_text") or ""}
            """),
            mo.md(f"""
            | | |
            |---|---|
            | Journey | {_journey.get("from_name") or "?"} → {_journey.get("to_name") or "?"} |
            | Operator / line | {_context.get("operator") or "-"} / {_context.get("line") or "-"} |
            | Departure | {_context.get("departure_time") or "-"} |
            | Situation | `{_context.get("situation_number") or "-"}` |
            | Wording came from | {_context.get("display_source") or "-"} ({_context.get("message_status") or "-"}) |
            | Writer | {_context.get("llm_provider") or "-"} / {_context.get("llm_model") or "-"} · prompt `{_context.get("prompt_version") or "-"}` |
            """),
            _blob(_context.get("prediction"), "prediction"),
            _blob(_context.get("policy"), "policy"),
            _blob(_context.get("delivery"), "delivery"),
            # The exact document the wording was written from — byte-identical to the prompt.
            # It carries origin, destination and departure time, which is why it is collapsed
            # rather than printed: it is the most identifying thing on the page.
            _blob(_context.get("message_input"), "message_input (the writer's own input)"),
        ])

    mo.vstack([_head, _body])
    return


@app.cell(hide_code=True)
def _(mo):
    mo.md("""
    ## Issues, and what the filter removed
    """)
    return


@app.cell(hide_code=True)
def issues_view(kept_issues, mo):
    # Read these before trusting the counts above: a response whose context is missing or
    # expired is in `kept` but is not reviewable, and a conflicting duplicate was never
    # enriched at all.
    mo.ui.table(kept_issues, selection=None, show_download=False, label="") if kept_issues \
        else mo.md("_No issues in this window._")
    return


@app.cell(hide_code=True)
def dropped_view(dropped, mo):
    # Always rendered, never hidden behind a toggle. A filter on free text can only be
    # trusted if what it removed is as visible as what it kept.
    _rows = [
        {
            "submitted": str(record["feedback"].get("submitted_at") or "")[:19],
            "rating": record["feedback"].get("rating"),
            "comment": record["feedback"].get("comment") or "",
        }
        for record in dropped
    ]
    mo.vstack([
        mo.md(f"**Filtered out as synthetic ({len(dropped)})** — check nothing real is here."),
        mo.ui.table(_rows, selection=None, show_download=False, label=""),
    ]) if dropped else mo.md("_Nothing matched the synthetic pattern._")
    return


@app.cell(hide_code=True)
def _(mo):
    mo.md("""
    ## Save to a file

    Off by default. The export file is the most sensitive artifact in this repo — free text
    beside a route — so it exists only when you decide it should. Saving writes the
    **unfiltered** document through the exporter's own `write_json`: mode `0600`, no
    stdout, no symlinks, no overwriting. Filtering is a reading convenience, and an
    evidence file that quietly omits rows is not evidence. Delete it within 30 days using
    the team's approved secure-deletion procedure.
    """)
    return


@app.cell(hide_code=True)
def save_controls(mo):
    save_path = mo.ui.text(
        value="",
        label="Absolute path (e.g. /private/tmp/varsel-feedback-2026-08-28.json)",
        full_width=True,
    )
    save = mo.ui.run_button(label="Write file", kind="warn")
    mo.vstack([save_path, save])
    return save, save_path


@app.cell(hide_code=True)
def save_file(
    Path,
    environment,
    exporter,
    from_date,
    issues,
    mo,
    responses,
    save,
    save_path,
    to_date,
):
    mo.stop(not save.value, mo.md(""))
    mo.stop(not save_path.value.strip(), mo.md("_Give it a path first._"))
    # Both branches assign and the cell ends on a bare `_outcome`, rather than each branch
    # ending on its own mo.md(...). marimo renders only an UNNESTED trailing expression, so the
    # nested form silently displays nothing — you press the button and the notebook looks
    # inert whether it wrote the file or refused to. `marimo check` catches this (branch-expression);
    # ruff and the Python interpreter do not.
    try:
        exporter.write_json(
            Path(save_path.value.strip()),
            {
                "metadata": {
                    "environment": environment.value,
                    "from_date": from_date.value,
                    "to_date_exclusive": to_date.value,
                    "response_count": len(responses),
                    "issue_count": len(issues),
                },
                "responses": responses,
                "issues": issues,
            },
        )
        _outcome = mo.md(f"Wrote `{save_path.value.strip()}` (mode 0600).")
    except (FileExistsError, FileNotFoundError, ValueError, OSError) as _exc:
        _outcome = mo.md(f"/// attention | Not written\n`{_exc}`\n///")
    _outcome
    return


if __name__ == "__main__":
    app.run()
