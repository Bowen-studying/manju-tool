# ProductionRun M3.4.1 Audit Integrity Retest

M3.4.1 hardens recovery and audit validation after the successful M3.4 paid
source and style revision tests. It does not change Provider request or paid
execution behavior, so this retest requires no new image generation call.

## Fixed boundaries

- `status`, `advance`, and `doctor` verify every runtime input in the active
  contract, including `style.reference`.
- Audit export and external-key verification validate every historical
  `run_created` contract and all of its runtime snapshots.
- Every stage-private `.runtime_inputs/*.bin` file must match an authorized
  contract or stage artifact reference and its content-addressed filename.
- Source bootstrap can recover if registration succeeds but selection append
  is interrupted.

## Offline verification

```powershell
.\.venv\Scripts\python.exe -m compileall -q manju tests
.\.venv\Scripts\python.exe -m pytest -q tests/test_production_m3_4_artifact_driven_revision.py
.\.venv\Scripts\python.exe -m pytest -q tests/test_production_m2_8_auditability.py tests/test_production_m3_3_runtime_reuse.py tests/test_production_m3_2_revision_paid_closure.py
.\.venv\Scripts\python.exe -m pytest -q
```

Expected focused result: `22 passed`. The development worktree full result is
`493 passed, 3 skipped, 1 failed`; the single failure is caused only by two
untracked user documents outside this delivery.

## Operator HMAC retest

Use disposable copies of both M3.4 paid project folders and the same external
HMAC key identifier used when those projects were created. Do not call the
Provider and do not create a new revision.

1. Export a fresh audit snapshot from each copied `project/project.json`.
2. Verify each new snapshot with `audit verify --verify-hmac` and confirm
   `manifest_valid=true` and `hmac_verified=true`.
3. In a separate disposable copy, alter one predecessor
   `production/runs/<run>/inputs/*.bin`; audit export must fail.
4. Restore the project, alter one stage `.runtime_inputs/*.bin`; audit export
   must fail.
5. Preserve the command output, new audit snapshots, and checksums. Do not put
   the HMAC key or Provider credentials in the evidence folder.

No paid API call is needed for this retest because the affected code only
validates stored evidence and bootstrap recovery.
