# ProductionRun M4.1 Testing

## Scope

M4.1 adds an optional bounded LangGraph `voice_director` stage after the
deterministic M4.0 `voice_script` stage. It produces only the immutable
`voice_direction.main` artifact. The acceptance implementation uses the local
deterministic model port; it does not call an LLM endpoint, TTS, Provider,
approval, grant, operation, or billing system.

The stage consumes exact, stage-private snapshots of:

- `storyboard.output`
- `voice_script.main`
- `voice_director.policy`

The LangGraph checkpoint is child-run recovery evidence only. ProductionRun's
event ledger, contract, artifact graph, and terminal authority remain the
cross-stage authority.

## Local acceptance

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests/test_production_m4_1_voice_director.py
.\.venv\Scripts\python.exe -m pytest -q tests/test_production_m4_0_voice_script.py
.\.venv\Scripts\python.exe -m pytest -q
```

The focused suite covers explicit M4.1 DAG sequencing, one-to-one cue mapping,
offline model isolation, durable model-call budget, checkpoint/output tamper
blocking, exact three-input artifact dependencies, path-free DTOs, and reuse
without a second model call.

## Deferred

Remote or paid LLM calls, TTS, human voice casting, audio generation, and
Provider settlement are outside M4.1. They require a separate M4.2 contract
and must not be implemented by bypassing the existing approval/grant/operation
boundary.
