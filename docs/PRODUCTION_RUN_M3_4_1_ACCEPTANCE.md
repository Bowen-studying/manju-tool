# ProductionRun M3.4.1 Acceptance

Date: 2026-08-17

M3.4.1 is accepted as the frozen artifact-driven revision and audit-integrity
baseline.

## Accepted results

- Clean archive: `496 passed, 1 skipped, 0 failed` on Python 3.12.
- Focused M3.4.1 verification: `22 passed`.
- Source and style paid-project copies each exported a fresh audit snapshot.
- Both fresh snapshots passed their manifests and HMAC implementation checks.
- The retained command log directly shows predecessor and stage-private
  tampering failing closed for the source project.
- The supplied operator report records the same two fail-closed checks for the
  style project; its raw style tamper commands are not in the evidence folder.
- The retest made zero Provider calls and created no revision.
- The outer evidence manifest verified `71/71` files.

## Trust statement

The retest used a disclosed fixture signing value. Its HMAC results demonstrate
deterministic signing, verification, and tamper detection, but they do not prove
exclusive operator identity or production-grade key custody. A production
deployment must use a private managed key that is absent from reports, command
logs, project files, and evidence bundles.

The evidence bundle contains no production credential. Contractual-tariff
records continue to represent signed pre-agreed prices rather than observed
upstream costs.

## Closed scope

M3.4.1 closes:

- selected source/style inputs driving successor execution;
- stage outputs updating the artifact graph;
- content-bound visual approval prompts;
- historical runtime-input and stage-private evidence validation;
- restart-safe source artifact bootstrap;
- backward-compatible M2.3 visual authority inspection.

Direct `storyboard.output` and `visual.asset` candidates remain intentionally
unsupported without a producer-run authority contract.

## Next phase

M4.0 should add only the free, deterministic voice-script stage. Its contract
should define storyboard input binding, immutable
voice-script output, artifact-graph dependencies, stage reuse, and frontend-safe
status DTOs. Paid TTS authorization belongs to a later increment after the
offline voice-script stage is frozen.
