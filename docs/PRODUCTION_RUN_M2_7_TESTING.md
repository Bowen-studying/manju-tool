# ProductionRun M2.7 Contractual Tariff Testing

M2.7 supports two mutually exclusive visual settlement modes.

- `provider_evidence` validates provider billing evidence and is the only mode that accepts `reconcile-manual`.
- `contractual_tariff` validates the pre-agreed price frozen into the approved, signed Grant. It does not establish, estimate, or claim the provider's actual upstream cost.

For `contractual_tariff`, the project must be initialized with a tariff ID and a tariff amount no greater than the approved visual budget:

```powershell
manju project init --source .\source.txt --source-type script --output-dir .\project --visual-provider-profile sync-image --visual-request-file .\request.json --visual-max-amount 5 --visual-settlement-mode contractual_tariff --visual-contractual-tariff-id image-contract-2026-08 --visual-contractual-tariff-amount 3
```

Complete normal approval, Grant issuance, manual dispatch, worker execution, and result import. The imported operation remains blocked as `outcome_unknown`. Settle it with no caller-provided amount, currency, bill, or provider reference:

```powershell
manju settle-manual-contractual-tariff .\project\project.json --operation-id <operation-id> --expected-last-event-hash <hash> --json
manju run .\project\project.json --json
```

The final operation and visual receipt must disclose all of the following:

```text
cost_source=contractual_tariff
settlement_mode=contractual_tariff
cost_disclosure=pre_agreed_price_not_upstream_actual_cost
```

`visual_receipt.json` is re-signed after settlement and `visual_authority.json` binds the settlement disclosure. A retry after a process interruption is safe: receipt sealing is idempotent and an existing matching manual settlement event is reused before `call_reconciled` is appended. A conflicting settled receipt or event is rejected.

Run offline verification without provider credentials or network access:

```powershell
.\.venv\Scripts\python.exe -m compileall -q manju tests
.\.venv\Scripts\python.exe -m pytest -q tests/test_production_m2_7_contractual_tariff.py
.\.venv\Scripts\python.exe -m pytest -q tests/test_production_run.py tests/test_production_m2_contracts.py tests/test_production_m2_1_mock_visual.py tests/test_production_m2_2_provider_contracts.py tests/test_production_m2_3_binary_receipts.py tests/test_production_m2_4_provider_safety.py tests/test_production_m2_5_runtime_profiles.py tests/test_production_m2_6_manual_sync.py tests/test_production_m2_7_contractual_tariff.py
.\.venv\Scripts\python.exe -m pytest -q
```

The repository tests use fixture providers only. A live provider test remains operator-owned because it requires a controlled worker host, a single-use approved dispatch, provider credentials kept outside the project, and (only for `provider_evidence`) retrievable billing evidence.
