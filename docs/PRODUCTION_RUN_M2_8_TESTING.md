# ProductionRun M2.8 Contractual Tariff Operations

M2.8 retains `contractual_tariff` as an internal settlement mode. It validates a signed, pre-agreed contract price. It does not establish or claim the upstream Provider's actual cost.

New tariff contracts are signed with these fields:

```text
amount_minor=<integer in the currency's smallest unit>
currency=<ISO currency code>
amount_unit=minor
charge_policy=on_success
pricing_scope=per_operation
```

`on_success` means a successfully imported result settles at the signed tariff. A final `failed` result settles at zero. The operation still records the failure and never creates a visual artifact. Historical M2.7 tariff contracts remain readable with their original per-attempt semantics.

Initialize a new contractual-tariff project as before. The existing amount option is explicitly interpreted as minor units:

```powershell
manju project init --source .\source.txt --source-type script --output-dir .\project --visual-provider-profile sync-image --visual-request-file .\request.json --visual-max-amount 500 --visual-settlement-mode contractual_tariff --visual-contractual-tariff-id image-contract-2026-08 --visual-contractual-tariff-amount 300
```

After the controlled worker writes `manual_result.json`, inspect its durable worker-local claim without triggering a Provider call:

```powershell
manju-provider-worker .\manual-dispatch.json --state-dir D:\manju-worker-state --inspect-claim
```

The response is a stable JSON DTO with one of these states:

```text
unclaimed         -> execute_once
dispatch_started  -> reconcile_provider
result_written    -> import_result
invalid           -> do_not_retry
```

Any existing claim remains non-reusable. `result_written` binds the signed result outcome, result SHA-256, file name, and completion time. A transport error after `dispatch_started` remains `reconcile_provider`; do not retry the Provider request.

Export an evidence snapshot after importing and, where applicable, settling a result:

```powershell
$env:MANJU_PRODUCTION_HMAC_KEY = '<operator key>'
manju audit export .\project\project.json --destination D:\audit-snapshot --worker-result-dir D:\manju-result --worker-state-dir D:\manju-worker-state --json
manju audit verify D:\audit-snapshot --json
```

This creates an `evidence_snapshot`, not a self-contained recovery package. It excludes Provider credentials and HMAC keys. Export uses the external key to domain-sign the manifest, while the default verifier only checks the SHA-256 manifest. To authenticate the manifest and verify signed events, provide the externally managed HMAC key only through the process environment:

```powershell
$env:MANJU_PRODUCTION_HMAC_KEY = '<operator key>'
manju audit verify D:\audit-snapshot --verify-hmac --json
```

Run offline verification with no Provider credentials or network calls:

```powershell
.\.venv\Scripts\python.exe -m compileall -q manju tests
.\.venv\Scripts\python.exe -m pytest -q tests/test_production_m2_8_auditability.py
.\.venv\Scripts\python.exe -m pytest -q tests/test_production_run.py tests/test_production_m2_contracts.py tests/test_production_m2_1_mock_visual.py tests/test_production_m2_2_provider_contracts.py tests/test_production_m2_3_binary_receipts.py tests/test_production_m2_4_provider_safety.py tests/test_production_m2_5_runtime_profiles.py tests/test_production_m2_6_manual_sync.py tests/test_production_m2_7_contractual_tariff.py tests/test_production_m2_8_auditability.py
.\.venv\Scripts\python.exe -m pytest -q
```

A new live Provider call is operator-owned. Run it only after the offline and independent review gates pass, with a controlled worker host, one approved dispatch, a low budget, and no credential written into the project or audit snapshot.
