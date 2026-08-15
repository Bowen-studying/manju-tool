# ProductionRun M3.2 Low-Budget Revision Closure

M3.2 validates an already completed ProductionRun revision under the existing
M2.8 paid-operation boundary. Its offline test verifies the authorization and
ledger closure for one prospective, human-approved successor operation. Only
the operator-owned test performs a real Provider submission. It does not claim
to observe a Provider's upstream charge: the required settlement mode is
`contractual_tariff`.

## Fixed safety boundary

- Use a fresh disposable project directory; do not alter the M2.8 baseline or
  a historical production project.
- Use one changed artifact and at least one unaffected selected artifact. The
  revision preview must show the unaffected artifact in `reuse_manifest`.
- Set `maximum_paid_calls=1` and a low `maximum_amount`. The signed tariff
  amount must be no greater than that budget.
- Keep Provider credentials and `MANJU_PRODUCTION_HMAC_KEY` only in the
  operator/worker environment. Do not place either in the project, dispatch
  package, audit snapshot, terminal capture, or report.
- `contractual_tariff` is a signed pre-agreed price. Its required disclosure is
  `pre_agreed_price_not_upstream_actual_cost`; it is not upstream-cost proof.

## Required offline gate

Run from the repository root before any real dispatch:

```powershell
.\.venv\Scripts\python.exe -m compileall -q manju tests
.\.venv\Scripts\python.exe -m pytest -q tests/test_production_m3_2_revision_paid_closure.py
.\.venv\Scripts\python.exe -m pytest -q
```

The targeted test exercises a completed predecessor, immutable artifact
selection, revision creation, rejection of predecessor authority, successor
approval/Grant, one fixture-worker submission, contractual settlement, and
completion.

## Operator-owned real test

1. Create a disposable contractual-tariff project with one permitted paid call
   and complete its predecessor run. Register/select the predecessor artifact
   versions with `manju artifact register` and `manju artifact select`.
2. Register/select the changed version, then call `manju revision preview`.
   Review `changed`, `affected_artifacts`, `reused_artifacts`, predecessor run,
   fingerprint, and `last_event_hash`. Stop if the scope exceeds one intended
   paid operation.
3. Call `manju revision create` with the exact preview fingerprint and last
   event hash. Verify `manju revision list` shows a distinct successor run and
   the expected reuse manifest.
4. Run the successor until it is `awaiting_approval`. Review the scope,
   approve the successor request, and issue a new Grant. Never reuse the
   predecessor approval or Grant.
5. Prepare exactly one manual dispatch. The controlled worker may perform one
   Provider submission only. If its durable claim is `dispatch_started`, first
   reconcile with the Provider; do not resend the request.
6. Import the result, settle with `settle-manual-contractual-tariff`, run to
   completion, then export and verify an HMAC-backed audit snapshot.

## Pass/fail criteria

Pass only if the predecessor stays readable and immutable; the successor has a
new run, approval, Grant, operation ID, and one Provider submission; only the
changed dependency set is regenerated; reused hashes match the manifest; the
final receipt discloses the contractual tariff; and the audit snapshot verifies.

Stop immediately on an invalid preview, unexpected affected scope, failed
event/HMAC validation, budget/call count above one, unknown worker claim, or
uncertain Provider submission. Preserve the project and worker state for
reconciliation rather than retrying the Provider call.
