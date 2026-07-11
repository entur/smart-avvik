# Residual Review Findings

Source: ce-code-review run `20260711-015331-c87c3225` on branch `feat/reisevalg-varsel-feasibility`
(12 findings validated; 8 applied in commit `fix(review): apply validated code-review findings`;
the 4 below were deferred to the tracker — refactors or behavior changes needing design sign-off).

## Residual Review Findings

- **P2** `disruptions/api/main.py:522` — Share the (operator, jkey) journey-ref resolver between `/journey` and the reisevalg snapshot lookup (duplicated algorithm, already-diverged behavior) → [#13](https://github.com/entur/kix-avvik/issues/13)
- **P2** `disruptions/api/reisevalg.py:360` — Type the `svv_baselines` cells (frozen dataclass) and resolve the dead `p10_volume` field → [#14](https://github.com/entur/kix-avvik/issues/14)
- **P2** `disruptions/api/reisevalg.py:1114` — Volume-'normal' is exempt from the precision gate — misleading `NO_ADVANTAGE` from pre-rush data on congested mornings (symmetric gating needs product sign-off) → [#15](https://github.com/entur/kix-avvik/issues/15)
- **P3** `disruptions/api/reisevalg.py:822` — Live volume path has no holiday awareness — guaranteed false 'congested' on Mon–Fri public holidays; mandatory re-check before `VOLUME_NUDGE_ENABLED` is ever opened → [#16](https://github.com/entur/kix-avvik/issues/16)
