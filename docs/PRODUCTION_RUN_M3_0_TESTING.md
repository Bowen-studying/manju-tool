# ProductionRun M3.0 Artifact Graph Testing

M3.0 introduces only the immutable artifact-graph foundation. It does not
create a successor run, issue a new grant, or invoke a Provider. Those actions
belong to the subsequent revision milestone.

## Contract

- `artifact register` hashes an existing project-relative file and records an
  immutable `logical_id + sha256 version_id` event.
- `artifact select` can select only an available registered version. It records
  the old selected version and an exact transitive dependency invalidation set.
- `production/artifacts.json` is a projection. It is rebuilt from the top-level
  hash chain and cannot be treated as authority.
- Every graph read recomputes selection invalidation; a recomputed or forged
  event payload is rejected even if its outer hash chain is otherwise valid.
- Every graph read verifies registered file bytes against the recorded SHA-256
  and rejects symlinks and Windows reparse points in artifact paths.

## Automated verification

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests/test_production_m3_0_artifact_graph.py
.\.venv\Scripts\python.exe -m pytest -q tests/test_production_run.py tests/test_production_m2_contracts.py tests/test_production_m2_1_mock_visual.py tests/test_production_m2_2_provider_contracts.py tests/test_production_m2_3_binary_receipts.py tests/test_production_m2_4_provider_safety.py tests/test_production_m2_5_runtime_profiles.py tests/test_production_m2_6_manual_sync.py tests/test_production_m2_7_contractual_tariff.py tests/test_production_m2_8_auditability.py tests/test_production_m3_0_artifact_graph.py
```

## CLI smoke test

Create a project, place a test file below its `outputs/` directory, then use
the `last_event_hash` returned by each write:

```powershell
manju artifact register .\project\project.json --logical-id source.script --path outputs\source-v1.txt --producer-stage source --expected-last-event-hash <hash> --json
manju artifact select .\project\project.json --logical-id source.script --version-id sha256:<hash> --expected-last-event-hash <hash> --json
manju artifact status .\project\project.json --json
```

No live API test is required for M3.0: the feature performs no Provider call.
