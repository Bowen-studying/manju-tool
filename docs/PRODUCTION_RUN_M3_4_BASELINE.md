# ProductionRun M3.4 Baseline

Date: 2026-08-17

This document freezes the M3.4 artifact-driven revision baseline after offline
and real paid verification. Later milestones must preserve replay compatibility
for the contracts, events, authorities, and artifact graphs frozen here.

## Verified scope

- Selected `source.script` and `style.reference` versions are snapshotted into
  the successor contract and drive stage execution.
- Storyboard and visual terminal events commit their immutable outputs and exact
  dependencies into the artifact graph.
- Visual approval prompts bind verified storyboard content and optional style
  content before any paid Provider side effect.
- Reused storyboard authority remains provenance-only and creates no duplicate
  approval, grant, reservation, or Provider call.
- Historical `visual-adapter-m2-3-v1` authorities remain readable after restart.
- Stage-private inputs remain credential-free audit evidence.

Direct replacement candidates for `storyboard.output` and `visual.asset` are
deliberately rejected until an explicit producer-run authority model exists.

## Verification evidence

The pre-hardening clean-baseline offline result was `495 passed, 1 skipped, 0
failed` on Python 3.12. The hardened development worktree result was `493
passed, 3 skipped, 1
failed`; its only failure came from two unrelated untracked documents scanned
by the public-content compliance test.

Two disposable real paid successor runs completed:

- Source revision: one successor Provider submission and current
  `source v2 -> storyboard v2 -> visual v2`, with reused style v1.
- Style-only revision: reused storyboard, one successor Provider submission,
  and current visual depending on storyboard v1 plus style v2.

The exported outer manifests verified `81/81` and `75/75` files. Both inner
audit manifests verified locally. The supplied M3.4.1 report records successful
HMAC verification for both paid project copies. The report also discloses that
the signing value is a test fixture, so this evidence proves signature and
tamper-detection mechanics but does not establish exclusive operator identity.
No production key or Provider credential is part of the evidence bundles.

Contractual-tariff settlement proves the signed, pre-agreed price only. It is
not evidence of the Provider's upstream actual cost.

## Reproducible verification

```powershell
.\.venv\Scripts\python.exe -m compileall -q manju tests
.\.venv\Scripts\python.exe -m pytest -q tests/test_production_m3_4_artifact_driven_revision.py
.\.venv\Scripts\python.exe -m pytest -q tests/test_production_m3_3_runtime_reuse.py tests/test_production_m3_2_revision_paid_closure.py tests/test_production_m2_8_auditability.py
.\.venv\Scripts\python.exe -m pytest -q
```

## Next milestone boundary

The artifact/revision milestone is closed at M3.4.1. The next planned runtime
milestone is M4.0 deterministic offline voice-script integration, following
the architecture order in `PRODUCTION_RUN_V1_ARCHITECTURE.md`. Paid TTS is a
later increment after the offline stage is frozen. Direct storyboard/visual output
replacement remains deferred until an explicit producer-run authority model is
specified. Later work must not infer authority from an artifact hash alone,
mutate a completed predecessor, or move paid side effects outside the approval
and grant boundary.
