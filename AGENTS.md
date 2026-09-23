# AGENTS.md

This file provides guidance to Codex (Codex.ai/code) when working with code in this repository.

## Read `CLAUDE.md` — it is the guidance, and this file is a pointer to it

Everything an agent needs about this repository — the project, the tooling, the BigQuery tables, the
deployment, every front-end and API surface, and the conventions — lives in
[`CLAUDE.md`](CLAUDE.md) at the repo root and, per surface, in [`.claude/rules/`](.claude/rules/)
(the root file's index says which file covers what). **Read `CLAUDE.md`, then the rule file for the
surface you are working on. Nothing is duplicated here.**

### Why this is a pointer and not a second copy

This file used to be a hand-maintained copy of `CLAUDE.md`, and on 2026-09-09 it stood at 405 lines
against that file's 1630 — roughly a quarter of the content, differing by about 1 741 diff lines, and
stale since well before the branch that changed it. That is what a hand-synchronised duplicate of a
long, dense, frequently-edited document decays into: every change has to be made twice, one of the
two is written first, and the other is the one a later reader happens to open.

The failure is worse than "out of date". The two files disagreed about **what exists** — surfaces,
flags and tables were described in one and absent from the other — so an agent reading this one would
plan against a repository that had moved on, and would do so confidently, because nothing in a stale
document announces itself as stale. Re-synchronising by hand would only have reset the clock on the
same failure.

So: one document, one place it is edited, and this pointer. A plan that says "update `CLAUDE.md` and
`AGENTS.md`" now means updating `CLAUDE.md` (or the `.claude/rules/` file for that surface) alone.
