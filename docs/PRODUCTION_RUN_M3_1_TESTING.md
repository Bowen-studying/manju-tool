# ProductionRun M3.1 Revision Successor Runs

M3.1 turns a reviewed graph change into an immutable successor-run contract.
It does not reuse a prior Grant and it does not create a Provider call.

## Contract

1. `revision preview` accepts only current artifact versions and produces a
   fingerprint bound to the predecessor run, graph state, and event hash.
2. `revision create` requires that exact preview fingerprint and last hash.
   Any intervening graph or ledger write invalidates the preview.
3. The new contract records `predecessor_run_id`, `revision_id`, and a stable
   `reuse_manifest`. The prior run remains historical; the successor becomes
   active through a new `run_created` event.
4. A successor has no inherited approval, Grant, reservation, or Provider
   operation. New paid work must pass the normal M2 approval and Grant flow.

## Automated verification

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests/test_production_m3_1_revisions.py
.\.venv\Scripts\python.exe -m pytest -q
```

## CLI smoke test

Use a current artifact version from `manju artifact status`:

```powershell
manju revision preview .\project\project.json --changed '{"logical_id":"source.script","version_id":"sha256:..."}' --json
manju revision create .\project\project.json --changed '{"logical_id":"source.script","version_id":"sha256:..."}' --requested-by operator --reason "source update" --preview-fingerprint <fingerprint> --expected-last-event-hash <hash> --json
manju revision list .\project\project.json --json
```

Run the successor only after its revision scope is reviewed. A low-budget real
Provider revision belongs after offline tests and independent review pass.
