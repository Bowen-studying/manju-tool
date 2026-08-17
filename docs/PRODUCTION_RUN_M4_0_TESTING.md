# ProductionRun M4.0 Testing

## Scope

M4.0 adds an opt-in, offline `voice_script` stage. It runs after storyboard and
before visual when both are enabled. It emits one `voice_script.main` artifact
that depends only on the exact `storyboard.output` version.

The adapter preserves scene and shot array order. It copies explicit dialogue
and narration without rewriting. A legacy `dialogue_narration` value is used
only when both structured fields are empty. M4.0 does not infer emotion, cast a
voice, call an LLM, generate audio, or create approval and billing events.

Enable the stage for a new project with `manju project init --voice-script`.
Existing M1 and M2 projects retain their frozen DAG behavior.

## Automated acceptance

Run from the repository virtual environment:

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests/test_production_m4_0_voice_script.py
```

The focused suite verifies:

- deterministic cue content, ordering, timing context, and content hashes;
- structured dialogue, narration, legacy input, and silent shots;
- stage-private storyboard binding and fail-closed artifact inspection;
- M4 DAG progress and free-stage execution before visual approval;
- absence of approval, grant, operation, Provider, LLM, and TTS side effects;
- exact artifact-graph dependency, revision regeneration, and stage reuse;
- restart validation, tamper blocking, and path-free frontend DTOs.

Run the full repository suite before freezing the milestone:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

## Deferred

M4.1 may introduce a bounded LangGraph voice-director Agent for emotion,
prosody, casting requirements, and review. M4.2 may introduce paid TTS. Neither
capability is evidence of M4.0 acceptance and neither may mutate
`voice_script.main` retroactively.
