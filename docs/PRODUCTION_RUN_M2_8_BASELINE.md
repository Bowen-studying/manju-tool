# ProductionRun M2.8 Baseline

Date: 2026-08-14

This document freezes the code baseline that completed the M2.8 contractual
tariff, manual-worker claim, and audit-snapshot milestone before M3 artifact
graph work begins.

## Verified scope

- ProductionRun M1/M2 orchestration, event-chain validation, approvals,
  grants, manual synchronization, contractual-tariff settlement, and audit
  snapshot export/verification.
- The CLI commands and package scripts required by the M1 through M2.8 test
  instructions.
- The M1/M2 production test suite and its acceptance/testing contracts.

The baseline deliberately excludes historical delivery folders, previous
real-API retest reports, and unrelated storyboard/visual feature experiments.

## Reproducible verification

Run from the repository root using the project virtual environment:

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests/test_production_run.py tests/test_production_m2_contracts.py tests/test_production_m2_1_mock_visual.py tests/test_production_m2_2_provider_contracts.py tests/test_production_m2_3_binary_receipts.py tests/test_production_m2_4_provider_safety.py tests/test_production_m2_5_runtime_profiles.py tests/test_production_m2_6_manual_sync.py tests/test_production_m2_7_contractual_tariff.py tests/test_production_m2_8_auditability.py
```

Result at freeze: `120 passed` on Python 3.10.11 and pytest 8.4.2.

## Security and settlement boundary

`contractual_tariff` settles a signed, pre-agreed price only. It must expose
`pre_agreed_price_not_upstream_actual_cost` and must never be represented as a
Provider's observed upstream charge.

Audit snapshots are credential-free evidence snapshots. Their HMAC key is
external to the snapshot, so a snapshot is recoverable only together with the
operator-managed key material. A production deployment must use a managed,
non-test key identifier.

## M3 entry point

M3 starts from this commit with an immutable artifact graph, revisions that
create successor runs, and deterministic selective invalidation. Existing M2
event history stays authoritative; derived projections remain rebuildable.
