# ProductionRun M3.4 Artifact-Driven Revision Testing

M3.4 closes the runtime path for a M3.3 revision. A successor now snapshots
its selected immutable inputs under its run directory, executes storyboard from
that snapshot, commits stage outputs into the artifact graph from the terminal
event, and renders the approved visual request from the verified storyboard
and optional style artifact.

## Offline verification

```powershell
.\.venv\Scripts\python.exe -m compileall -q manju tests
.\.venv\Scripts\python.exe -m pytest -q tests/test_production_m3_4_artifact_driven_revision.py
.\.venv\Scripts\python.exe -m pytest -q tests/test_production_m3_3_runtime_reuse.py tests/test_production_m3_2_revision_paid_closure.py
.\.venv\Scripts\python.exe -m pytest -q tests/test_production_m2_8_auditability.py
.\.venv\Scripts\python.exe -m pytest -q
```

## 2026-08-17 verification results

`compileall` passed. After the M3.4.1 audit hardening, the M3.4 suite passed
(`8 passed`), the M3.3 runtime
reuse suite passed (`5 passed`), the M3.2 revision suite passed (`1 passed`),
the M2.1 visual suite passed (`10 passed`), and the M2.8 auditability suite
passed (`8 passed`). The full suite result was `493 passed, 3 skipped, 1
failed`; its only failure is the pre-existing public-document compliance scan
of two untracked user documents under `docs/`, which are outside this change.

A clean M3.3 baseline with only the M3.4 delivery applied passed the complete
suite with `495 passed, 1 skipped, 0 failed` on Python 3.12. The difference is
explained by two Windows-only skips and the two unrelated untracked documents
present in the development worktree.

Two disposable contractual-tariff projects then completed the real paid
verification. The source revision regenerated storyboard and visual with one
successor Provider submission. The style-only revision reused storyboard and
regenerated visual with one successor Provider submission. Both exported audit
snapshots passed their manifests. The supplied report records successful
external-key HMAC verification; that step cannot be independently repeated
without the operator-held key. Contractual tariff amounts remain pre-agreed
prices, not observed upstream costs.

The M3.4-specific suite verifies these properties:

1. A source candidate is copied into the successor input snapshot and the
   storyboard adapter receives that selected content.
2. The visual approval prompt and its request fingerprint change with the
   regenerated storyboard and bind its exact source/style versions.
3. A source revision commits `source v2 -> storyboard v2 -> visual v2` as the
   current graph while a style-only revision reuses storyboard without a
   storyboard approval and regenerates visual with the new style version.
4. Identical output bytes remain replayable after a new input version, and an
   altered registered candidate is rejected before any successor stage runs.
5. Stage-private input copies are included as credential-free `.bin` evidence
   files, so completed M3.4 projects continue to export and verify audit
   snapshots.

## Real paid verification

Run only after the offline suite. Use a fresh, low-budget contractual-tariff
project and preserve the complete project, worker-result, worker-state, audit
export, and checksum manifest.

1. Complete a predecessor with `source.script`, `style.reference`,
   `storyboard.output`, and `visual.asset` current.
2. Register a `source.script v2` candidate, preview it, and create the
   successor. Inspect its `contract.json`: `runtime_inputs.source.script`
   must point to a run-local snapshot with the selected v2 hash.
3. Before approval, confirm the successor visual request contains the new
   storyboard material and has a different provider-request fingerprint from
   the predecessor. Approve exactly one visual operation and reconcile it.
4. Verify the final artifact graph has current source v2, storyboard v2, and
   visual v2, with visual depending on storyboard v2 and reused style v1.
5. In a separate fresh project, replace only style. Confirm storyboard is
   reused, visual is regenerated, and no duplicate storyboard authorization or
   provider operation is created.

Do not copy credentials, HMAC keys, or provider environment files into the
evidence bundle.
