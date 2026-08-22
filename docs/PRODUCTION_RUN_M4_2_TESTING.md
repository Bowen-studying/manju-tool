# ProductionRun M4.2 Testing

## Scope

M4.2 adds an optional offline fake TTS stage after the bounded M4.1
`voice_director` stage. It produces the immutable `voice_audio.main` artifact,
`voice_audio.json`, and a validated mono 16-bit 16 kHz WAV. The default adapter
is deterministic and never reaches a network, provider account, approval,
grant, operation, or billing system.

The stage consumes an exact stage-private snapshot of `voice_direction.main`.
The input version, model profile, receipt, WAV hash/size, manifest, authority,
and event-ledger terminal record are mutually bound. A durable call receipt is
reserved before synthesis and carries a stable idempotency key. Recovery retries
only through an idempotent model port, reusing the same key; unresolved or
tampered receipts fail closed. Cue count, cue duration, total duration, and
audio byte limits are enforced before publication.

## Local acceptance

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests/test_production_m4_2_voice_tts.py
.\.venv\Scripts\python.exe -m pytest -q tests/test_production_m4_1_voice_director.py
.\.venv\Scripts\python.exe -m pytest -q
```

The focused suite covers strict M4.2/M4.1 DAG compatibility, deterministic
offline synthesis, no paid-operation events, path-free status DTOs, exactly
one fake-model call, receipt recovery after audio-only, reserved-receipt, and
manifest-only publication crashes, model-profile drift, resource limits,
audio/manifest/input tamper rejection, and artifact graph dependency binding.

## Deferred paid-provider test

This increment deliberately does not claim a real paid TTS provider pass. A
provider adapter must first be connected to the existing approval, grant,
reservation, submission, reconciliation, settlement, and budget-ledger
boundary. It must be tested with provider credentials, a selected voice/model,
an approved budget, and a disposable account. The desktop handoff generated
for this run is the acceptance checklist for that future manual/provider test.
