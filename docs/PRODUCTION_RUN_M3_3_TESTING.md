# ProductionRun M3.3 Atomic Revision Snapshot and Runtime Reuse

M3.3 makes a revision candidate project-scoped and binds its selection to the
successor `run_created` event. A completed predecessor is never retroactively
selected as the producer of a replacement artifact.

## Contract

1. Register the original graph while its predecessor is still active. A
   completed predecessor may not run `artifact select`.
2. Register replacements with `artifact candidate`. A candidate is available,
   has no predecessor `producer.run_id`, and changes no current selection.
3. `revision preview` binds predecessor selection, candidate replacements,
   deterministic affected closure, successor selection, reuse manifest, stage
   execution plan, and the ledger tail hash.
4. `revision create` commits those facts in the successor `run_created` event.
   The artifact graph applies candidate selection only when that event exists.
5. A `reuse` stage reuses the predecessor's hash-verified authority files and
   emits no approval, Grant, reservation, or Provider call. A `regenerate`
   visual stage requires the usual successor-only approval and Grant.
6. Visual approval binds the regenerated storyboard plus exact successor source
   and style versions. This is an authorization/input contract; a Provider
   request payload still requires its own adapter-specific prompt generation.

## Offline verification

```powershell
.\.venv\Scripts\python.exe -m compileall -q manju tests
.\.venv\Scripts\python.exe -m pytest -q tests/test_production_m3_3_runtime_reuse.py
.\.venv\Scripts\python.exe -m pytest -q
manju artifact candidate --help
manju revision preview --help
```

The M3.3 tests cover both required branches:

- `source.script` replacement invalidates `storyboard.output` and
  `visual.asset`; the successor visual approval binds the new source and the
  reused style version.
- An unrelated metadata replacement reuses storyboard and visual, completes
  without an approval or Provider operation, and records predecessor reuse
  provenance.

## Operator-owned real test (only after offline and independent review pass)

Use a fresh low-budget contractual-tariff project. Register the graph before
predecessor completion:

```text
source.script -> storyboard.output -> visual.asset
style.reference -------------------> visual.asset
```

After predecessor completion, write the replacement under `outputs/` and use
the returned tail hash:

```powershell
manju artifact candidate .\project\project.json --logical-id source.script --path outputs\source-v2.txt --producer-stage revision_candidate --expected-last-event-hash <hash> --json
manju revision preview .\project\project.json --changed '{"logical_id":"source.script","version_id":"sha256:..."}' --json
manju revision create .\project\project.json --changed '{"logical_id":"source.script","version_id":"sha256:..."}' --requested-by operator --reason "source update" --preview-fingerprint <fingerprint> --expected-last-event-hash <hash> --json
```

Before a paid dispatch, verify that source replacement shows storyboard and
visual as `regenerate`, style as reused, and exactly one permitted paid visual
operation. A metadata-only replacement must show both stages as `reuse` and
must complete with zero Provider operations. Preserve worker state and reconcile
instead of retrying if a real worker reaches `dispatch_started`.
