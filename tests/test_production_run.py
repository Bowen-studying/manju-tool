import json
import os
import sqlite3
import threading
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner
from langgraph.checkpoint.base import empty_checkpoint
from langgraph.checkpoint.sqlite import SqliteSaver

from manju.cli import cli
from manju.pipeline.storyboard_supervisor import SUPERVISOR_AGENT_VERSION, SUPERVISOR_TOOLSET_VERSION
from manju.production.adapters.storyboard import StoryboardStageAdapter, storyboard_source_sha256
from manju.production.events import EventStore
from manju.production.locking import ProjectLock
from manju.production.models import ProductionError, ReasonCode
from manju.production.reducer import reduce_events
from manju.production.service import ProductionService, initialize_project
from manju.production.store import ProjectStore, sha256_file
from manju.utils.runtime import atomic_write_json


def _source(tmp_path: Path) -> Path:
    path = tmp_path / "story.txt"
    path.write_text("林夏冲上天台，身后的门突然打开。", encoding="utf-8")
    return path


def _initialize(tmp_path: Path):
    source = _source(tmp_path)
    project_dir = tmp_path / "project"
    snapshot = initialize_project(
        source=str(source),
        source_type="script",
        output_dir=str(project_dir),
        engine="agent",
        max_scenes=1,
    )
    return project_dir / "project.json", snapshot


def _write_stage_result(
    output_dir: str,
    status: str = "completed",
    *,
    source_path: str | None = None,
    engine: str = "agent",
    model: str = "mock-model",
    settings: dict | None = None,
    run_id: str = "child-run",
    checkpoint_thread_id: str | None = None,
    valid_checkpoint: bool = True,
) -> dict:
    os.makedirs(output_dir, exist_ok=True)
    checkpoint = os.path.join(output_dir, "stages", "agent", "checkpoints.sqlite")
    os.makedirs(os.path.dirname(checkpoint), exist_ok=True)
    source_sha256 = storyboard_source_sha256(source_path) if source_path else ""
    if valid_checkpoint:
        connection = sqlite3.connect(checkpoint)
        saver = SqliteSaver(connection)
        value = empty_checkpoint()
        value["channel_values"] = {
            "run_id": run_id,
            "status": status,
            "model_name": model,
            "source_sha256": source_sha256,
            "max_steps": (settings or {}).get("max_steps", 40),
            "requested_max_calls": (settings or {}).get("max_calls"),
            "max_revisions": (settings or {}).get("max_revisions", 2),
        }
        saver.put(
            {"configurable": {"thread_id": checkpoint_thread_id or run_id, "checkpoint_ns": ""}},
            value,
            {},
            {},
        )
        connection.close()
    else:
        Path(checkpoint).write_bytes(b"not-a-sqlite-checkpoint")
    Path(output_dir, "agent_trace.jsonl").write_text(
        json.dumps({"sequence": 1, "run_id": run_id, "action": "fixture"}) + "\n",
        encoding="utf-8",
    )
    storyboard = {
        "schema_version": "2",
        "title": "测试分镜",
        "metadata": {
            "agent_status": status if engine == "agent" else "",
            "generation_engine": engine,
            "source_sha256": source_sha256,
        },
        "scenes": [],
    }
    atomic_write_json(os.path.join(output_dir, "storyboard.json"), storyboard)
    atomic_write_json(os.path.join(output_dir, "agent_run.json"), {
        "run_id": run_id,
        "status": status,
        "stop_reason": "source_ambiguity" if status == "needs_review" else status,
        "checkpoint": "agent/checkpoints.sqlite",
        "trace": "agent_trace.jsonl",
        "model": model,
        "source_sha256": source_sha256,
        "supervisor_agent_version": SUPERVISOR_AGENT_VERSION,
        "toolset_version": SUPERVISOR_TOOLSET_VERSION,
        "budgets": {
            "max_steps": (settings or {}).get("max_steps", 40),
            "requested_max_calls": (settings or {}).get("max_calls") or "auto",
            "max_revisions_per_scene": (settings or {}).get("max_revisions", 2),
        },
    })
    return storyboard


class RecordingRunner:
    def __init__(self, status="completed"):
        self.status = status
        self.calls = []

    def __call__(self, source_path, **kwargs):
        self.calls.append((source_path, kwargs))
        return _write_stage_result(
            kwargs["output_dir"], self.status,
            source_path=source_path,
            engine=kwargs["engine"],
            settings={
                "max_steps": kwargs["agent_max_steps"],
                "max_calls": kwargs["agent_max_calls"],
                "max_revisions": kwargs["agent_max_revisions"],
            },
        )


def _service(project_file: Path, runner: RecordingRunner) -> ProductionService:
    return ProductionService(
        str(project_file),
        storyboard_adapter=StoryboardStageAdapter(runner=runner),
    )


def test_event_store_detects_tampering(tmp_path):
    path = tmp_path / "events.jsonl"
    store = EventStore(str(path))
    store.append("project_initialized", project_id="prj_test", payload={"value": 1})
    event = json.loads(path.read_text(encoding="utf-8"))
    event["payload"]["value"] = 2
    path.write_text(json.dumps(event) + "\n", encoding="utf-8")

    with pytest.raises(ProductionError) as caught:
        store.read()
    assert caught.value.code == ReasonCode.PROJECT_EVENT_CHAIN_INVALID.value


def test_reducer_rejects_run_completed_without_stage_outcome(tmp_path):
    path = tmp_path / "events.jsonl"
    store = EventStore(str(path))
    store.append("project_initialized", project_id="prj_test")
    store.append("run_completed", project_id="prj_test", run_id="run_test")

    with pytest.raises(ProductionError) as caught:
        reduce_events(store.read())
    assert caught.value.code == ReasonCode.PROJECT_EVENT_CHAIN_INVALID.value


def test_reducer_rejects_empty_run_started_id_and_mixed_stage_ids(tmp_path):
    empty_run = EventStore(str(tmp_path / "empty-run.jsonl"))
    empty_run.append("project_initialized", project_id="prj_test")
    empty_run.append("run_created", project_id="prj_test", run_id="run_test")
    empty_run.append("run_started", project_id="prj_test", run_id="")
    with pytest.raises(ProductionError):
        reduce_events(empty_run.read())

    mixed = EventStore(str(tmp_path / "mixed.jsonl"))
    mixed.append("project_initialized", project_id="prj_test")
    mixed.append("run_created", project_id="prj_test", run_id="run_test")
    mixed.append("run_started", project_id="prj_test", run_id="run_test")
    mixed.append(
        "stage_scheduled", project_id="prj_test", run_id="run_test",
        payload={"stage": "storyboard", "stage_invocation_id": "invocation-a"},
    )
    mixed.append(
        "stage_run_attached", project_id="prj_test", run_id="run_test",
        payload={"stage": "storyboard", "stage_run_id": "child-b"},
    )
    mixed.append(
        "stage_completed", project_id="prj_test", run_id="run_test",
        payload={"stage": "storyboard", "stage_run_id": "child-c"},
    )
    with pytest.raises(ProductionError):
        reduce_events(mixed.read())


def test_projection_is_rebuilt_from_event_chain(tmp_path):
    project_file, _ = _initialize(tmp_path)
    store = ProjectStore(str(project_file))
    store.paths.state_file and os.unlink(store.paths.state_file)

    store.write_projection()

    projection = json.loads(Path(store.paths.state_file).read_text(encoding="utf-8"))
    assert projection["status"] == "ready"
    assert projection["last_event_hash"] == store.events.read()[-1]["event_hash"]


def test_status_rebuilds_missing_projection(tmp_path):
    project_file, _ = _initialize(tmp_path)
    store = ProjectStore(str(project_file))
    os.unlink(store.paths.state_file)

    snapshot = _service(project_file, RecordingRunner()).get_status()

    projection = json.loads(Path(store.paths.state_file).read_text(encoding="utf-8"))
    assert projection["last_event_hash"] == snapshot.last_event_hash


def test_run_completes_and_is_idempotent_without_media_calls(tmp_path):
    project_file, _ = _initialize(tmp_path)
    runner = RecordingRunner()
    service = _service(project_file, runner)

    with patch("manju.production.service.get_ai_config", return_value=(None, "mock-model", None)):
        first = service.run_until_blocked()
        second = service.run_until_blocked()

    assert first.status == "completed"
    assert second.status == "completed"
    assert len(runner.calls) == 1
    kwargs = runner.calls[0][1]
    assert kwargs["image_api"] is False
    assert kwargs["image_engine"] == "legacy"
    assert kwargs["resume"] is True
    assert first.reason.code == ReasonCode.PROJECT_ALREADY_COMPLETED.value


def test_needs_review_stops_without_reinvocation(tmp_path):
    project_file, _ = _initialize(tmp_path)
    runner = RecordingRunner(status="needs_review")
    service = _service(project_file, runner)

    with patch("manju.production.service.get_ai_config", return_value=(None, "mock-model", None)):
        first = service.run_until_blocked()
        second = service.run_until_blocked()

    assert first.status == "needs_review"
    assert first.reason.code == ReasonCode.STORYBOARD_REVIEW_REQUIRED.value
    assert second.status == "needs_review"
    assert len(runner.calls) == 1


def test_pause_then_run_records_resume_and_completes(tmp_path):
    project_file, _ = _initialize(tmp_path)
    runner = RecordingRunner()
    service = _service(project_file, runner)

    with patch("manju.production.service.get_ai_config", return_value=(None, "mock-model", None)):
        created = service.advance()
        paused = service.request_pause()
        completed = service.run_until_blocked()

    assert created.status == "running"
    assert paused.status == "paused"
    assert completed.status == "completed"
    event_types = [event["event_type"] for event in service.store.events.read()]
    assert "pause_requested" in event_types
    assert "run_paused" in event_types
    assert "run_resumed" in event_types


def test_pause_can_be_requested_while_storyboard_call_is_running(tmp_path):
    project_file, _ = _initialize(tmp_path)
    started = threading.Event()
    release = threading.Event()

    class SlowRunner(RecordingRunner):
        def __call__(self, source_path, **kwargs):
            started.set()
            assert release.wait(timeout=5)
            return super().__call__(source_path, **kwargs)

    service = _service(project_file, SlowRunner())
    with patch("manju.production.service.get_ai_config", return_value=(None, "mock-model", None)):
        service.advance()

    results = []
    errors = []

    def run_stage():
        try:
            with patch("manju.production.service.get_ai_config", return_value=(None, "mock-model", None)):
                results.append(service.advance())
        except Exception as exc:  # pragma: no cover - surfaced by assertion below
            errors.append(exc)

    worker = threading.Thread(target=run_stage)
    worker.start()
    assert started.wait(timeout=5)
    paused = service.request_pause()
    assert paused.status == "paused"
    release.set()
    worker.join(timeout=10)

    assert not worker.is_alive()
    assert errors == []
    assert results[0].status == "paused"
    event_types = [event["event_type"] for event in service.store.events.read()]
    assert event_types.index("pause_requested") < event_types.index("stage_completed")
    assert event_types.index("stage_completed") < event_types.index("run_paused")


def test_project_contract_change_is_rejected(tmp_path):
    project_file, _ = _initialize(tmp_path)
    service = _service(project_file, RecordingRunner())
    with patch("manju.production.service.get_ai_config", return_value=(None, "mock-model", None)):
        service.advance()

    project = json.loads(project_file.read_text(encoding="utf-8"))
    project["production"]["storyboard"]["max_scenes"] = 2
    atomic_write_json(str(project_file), project)

    with pytest.raises(ProductionError) as caught:
        service.advance()
    assert caught.value.code == ReasonCode.PROJECT_CONTRACT_CHANGED.value


def test_retry_after_contract_write_interruption_creates_one_valid_run(tmp_path):
    project_file, _ = _initialize(tmp_path)
    service = _service(project_file, RecordingRunner())
    original_write = atomic_write_json

    def fail_run_contract(path, value):
        if path.startswith(os.path.join(service.paths.production_dir, "runs")) and path.endswith("contract.json"):
            raise RuntimeError("interrupt before contract write")
        return original_write(path, value)

    with patch("manju.production.service.get_ai_config", return_value=(None, "mock-model", None)), \
         patch("manju.production.service.atomic_write_json", side_effect=fail_run_contract):
        with pytest.raises(RuntimeError, match="interrupt before contract write"):
            service.advance()

    assert not [event for event in service.store.events.read() if event["event_type"] == "run_created"]
    with patch("manju.production.service.get_ai_config", return_value=(None, "mock-model", None)):
        recovered = service.advance()
    assert recovered.status == "running"
    assert len([event for event in service.store.events.read() if event["event_type"] == "run_created"]) == 1


def test_retry_after_run_created_interruption_resumes_with_run_started(tmp_path):
    project_file, _ = _initialize(tmp_path)
    runner = RecordingRunner()
    service = _service(project_file, runner)
    original_append = service.store.events.append
    interrupted = False

    def interrupt_after_run_created(event_type, **kwargs):
        nonlocal interrupted
        event = original_append(event_type, **kwargs)
        if event_type == "run_created" and not interrupted:
            interrupted = True
            raise RuntimeError("interrupt after run_created")
        return event

    with patch("manju.production.service.get_ai_config", return_value=(None, "mock-model", None)), \
         patch.object(service.store.events, "append", side_effect=interrupt_after_run_created):
        with pytest.raises(RuntimeError, match="interrupt after run_created"):
            service.advance()

    with patch("manju.production.service.get_ai_config", return_value=(None, "mock-model", None)):
        completed = service.run_until_blocked()

    event_types = [event["event_type"] for event in service.store.events.read()]
    assert completed.status == "completed"
    assert event_types.count("run_created") == 1
    assert event_types.count("run_started") == 1
    assert len(runner.calls) == 1


def test_retry_after_stage_scheduled_interruption_executes_once(tmp_path):
    project_file, _ = _initialize(tmp_path)
    runner = RecordingRunner()
    service = _service(project_file, runner)
    with patch("manju.production.service.get_ai_config", return_value=(None, "mock-model", None)):
        snapshot = service.advance()
    stage_run_id = f"storyboard-{snapshot.run_id.removeprefix('run_')}"
    service.store.events.append(
        "stage_scheduled",
        project_id=snapshot.project_id,
        run_id=snapshot.run_id,
        payload={"stage": "storyboard", "stage_invocation_id": stage_run_id},
    )

    with patch("manju.production.service.get_ai_config", return_value=(None, "mock-model", None)):
        completed = service.run_until_blocked()

    assert completed.status == "completed"
    assert len(runner.calls) == 1


def test_recovery_after_stage_completed_before_projection_does_not_rerun_agent(tmp_path):
    project_file, _ = _initialize(tmp_path)
    runner = RecordingRunner()
    service = _service(project_file, runner)
    with patch("manju.production.service.get_ai_config", return_value=(None, "mock-model", None)):
        service.advance()
        with patch.object(service.store, "write_projection", side_effect=RuntimeError("projection interrupted")):
            with pytest.raises(RuntimeError, match="projection interrupted"):
                service.advance()

    with patch("manju.production.service.get_ai_config", return_value=(None, "mock-model", None)):
        completed = service.run_until_blocked()
    assert completed.status == "completed"
    assert len(runner.calls) == 1


def test_resume_after_pause_requested_interruption_completes(tmp_path):
    project_file, _ = _initialize(tmp_path)
    runner = RecordingRunner()
    service = _service(project_file, runner)
    with patch("manju.production.service.get_ai_config", return_value=(None, "mock-model", None)):
        snapshot = service.advance()
    service.store.events.append(
        "pause_requested", project_id=snapshot.project_id, run_id=snapshot.run_id, payload={}
    )

    with patch("manju.production.service.get_ai_config", return_value=(None, "mock-model", None)):
        completed = service.run_until_blocked()
    assert completed.status == "completed"
    assert "run_resumed" in [event["event_type"] for event in service.store.events.read()]
    assert len(runner.calls) == 1


def test_retry_after_run_completed_before_projection_is_idempotent(tmp_path):
    project_file, _ = _initialize(tmp_path)
    runner = RecordingRunner()
    service = _service(project_file, runner)
    with patch("manju.production.service.get_ai_config", return_value=(None, "mock-model", None)):
        service.advance()
        service.advance()
        with patch.object(service.store, "write_projection", side_effect=RuntimeError("final projection interrupted")):
            with pytest.raises(RuntimeError, match="final projection interrupted"):
                service.advance()

    with patch("manju.production.service.get_ai_config", return_value=(None, "mock-model", None)):
        completed = service.run_until_blocked()
    assert completed.status == "completed"
    assert len(runner.calls) == 1


def test_runtime_model_change_is_rejected_before_stage_call(tmp_path):
    project_file, _ = _initialize(tmp_path)
    runner = RecordingRunner()
    service = _service(project_file, runner)
    with patch("manju.production.service.get_ai_config", return_value=(None, "model-a", None)):
        service.advance()

    with patch("manju.production.service.get_ai_config", return_value=(None, "model-b", None)):
        with pytest.raises(ProductionError) as caught:
            service.advance()
    assert caught.value.code == ReasonCode.PROJECT_CONTRACT_CHANGED.value
    assert runner.calls == []


def test_stored_source_change_is_rejected(tmp_path):
    project_file, _ = _initialize(tmp_path)
    store = ProjectStore(str(project_file))
    project = store.load_project()
    Path(store.source_path(project)).write_text("被修改的来源", encoding="utf-8")

    with pytest.raises(ProductionError) as caught:
        _service(project_file, RecordingRunner()).advance()
    assert caught.value.code == ReasonCode.SOURCE_HASH_MISMATCH.value

    with pytest.raises(ProductionError) as status_error:
        _service(project_file, RecordingRunner()).get_status()
    assert status_error.value.code == ReasonCode.SOURCE_HASH_MISMATCH.value


def test_completed_child_run_is_reconciled_without_model_call(tmp_path):
    project_file, _ = _initialize(tmp_path)
    runner = RecordingRunner()
    service = _service(project_file, runner)
    with patch("manju.production.service.get_ai_config", return_value=(None, "mock-model", None)):
        snapshot = service.advance()

    run_id = snapshot.run_id
    stage_run_id = f"storyboard-{run_id.removeprefix('run_')}"
    output_dir = service.paths.storyboard_dir(run_id, stage_run_id)
    service.store.events.append(
        "stage_scheduled",
        project_id=snapshot.project_id,
        run_id=run_id,
        payload={"stage": "storyboard", "stage_invocation_id": stage_run_id},
    )
    project = service.store.load_project()
    _write_stage_result(
        output_dir, "completed", source_path=service.store.source_path(project)
    )

    with patch("manju.production.service.get_ai_config", return_value=(None, "mock-model", None)):
        completed = service.run_until_blocked()

    assert completed.status == "completed"
    assert runner.calls == []


def test_project_lock_rejects_second_writer(tmp_path):
    lock_path = tmp_path / "project.lock"
    with ProjectLock(str(lock_path)):
        with pytest.raises(ProductionError) as caught:
            with ProjectLock(str(lock_path)):
                pass
    assert caught.value.code == ReasonCode.PROJECT_LOCKED.value


def test_doctor_reports_event_chain_and_contract(tmp_path):
    project_file, _ = _initialize(tmp_path)
    runner = RecordingRunner()
    service = _service(project_file, runner)
    with patch("manju.production.service.get_ai_config", return_value=(None, "mock-model", None)):
        service.run_until_blocked()

    report = service.doctor()

    assert report["status"] == "passed"
    assert report["integrity_status"] == "passed"
    assert report["run_status"] == "completed"
    names = {check["name"] for check in report["checks"]}
    assert {"project_schema", "source_integrity", "event_chain", "run_contract", "storyboard_stage"} <= names


def test_doctor_distinguishes_integrity_from_failed_run(tmp_path):
    project_file, _ = _initialize(tmp_path)
    service = _service(project_file, RecordingRunner("failed"))
    with patch("manju.production.service.get_ai_config", return_value=(None, "mock-model", None)):
        failed = service.run_until_blocked()

    report = service.doctor()
    assert failed.status == "failed"
    assert report["status"] == "passed"
    assert report["integrity_status"] == "passed"
    assert report["run_status"] == "failed"


def test_failed_manifest_without_storyboard_is_execution_failure(tmp_path):
    project_file, _ = _initialize(tmp_path)
    runner = RecordingRunner()
    service = _service(project_file, runner)
    with patch("manju.production.service.get_ai_config", return_value=(None, "mock-model", None)):
        snapshot = service.advance()
    stage_run_id = f"storyboard-{snapshot.run_id.removeprefix('run_')}"
    output_dir = service.paths.storyboard_dir(snapshot.run_id, stage_run_id)
    _write_stage_result(
        output_dir,
        "failed",
        source_path=service.store.source_path(service.store.load_project()),
    )
    Path(output_dir, "storyboard.json").unlink()

    with patch("manju.production.service.get_ai_config", return_value=(None, "mock-model", None)):
        failed = service.run_until_blocked()
    assert failed.status == "failed"
    assert failed.reason.code == ReasonCode.STORYBOARD_FAILED.value
    assert runner.calls == []


def test_tampered_failed_manifest_is_rejected_as_integrity_failure(tmp_path):
    project_file, _ = _initialize(tmp_path)
    runner = RecordingRunner()
    service = _service(project_file, runner)
    with patch("manju.production.service.get_ai_config", return_value=(None, "mock-model", None)):
        snapshot = service.advance()
    stage_run_id = f"storyboard-{snapshot.run_id.removeprefix('run_')}"
    output_dir = service.paths.storyboard_dir(snapshot.run_id, stage_run_id)
    _write_stage_result(
        output_dir,
        "failed",
        source_path=service.store.source_path(service.store.load_project()),
    )
    Path(output_dir, "storyboard.json").unlink()
    manifest_path = Path(output_dir, "agent_run.json")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["model"] = "tampered-model"
    atomic_write_json(str(manifest_path), manifest)
    checkpoint = Path(output_dir, "stages", "agent", "checkpoints.sqlite")
    checkpoint.write_bytes(b"tampered-checkpoint")
    contract = service.store.validate_contract(service.store.load_project(), snapshot.run_id)
    inspected = service.storyboard_adapter.inspect(
        stage_run_id=stage_run_id,
        output_dir=output_dir,
        expected=service._storyboard_expected(contract),
    )
    assert inspected is not None
    assert inspected.reason_code == ReasonCode.STAGE_INTEGRITY_FAILED.value

    with patch("manju.production.service.get_ai_config", return_value=(None, "mock-model", None)):
        failed = service.run_until_blocked()
    assert failed.status == "failed"
    assert failed.reason.code == ReasonCode.STAGE_INTEGRITY_FAILED.value
    assert runner.calls == []


def test_failed_stage_authority_tamper_invalidates_status_and_doctor(tmp_path):
    project_file, _ = _initialize(tmp_path)
    service = _service(project_file, RecordingRunner("failed"))
    with patch("manju.production.service.get_ai_config", return_value=(None, "mock-model", None)):
        service.run_until_blocked()
    terminal = next(
        event for event in service.store.events.read()
        if event["event_type"] == "stage_failed"
    )
    authority = Path(service.paths.root) / terminal["payload"]["authority_path"]
    authority.unlink()

    with pytest.raises(ProductionError) as caught:
        service.get_status()
    assert caught.value.code == ReasonCode.STAGE_INTEGRITY_FAILED.value
    assert service.doctor()["integrity_status"] == "failed"


def test_completed_stage_manifest_deletion_invalidates_status_and_doctor(tmp_path):
    project_file, _ = _initialize(tmp_path)
    service = _service(project_file, RecordingRunner())
    with patch("manju.production.service.get_ai_config", return_value=(None, "mock-model", None)):
        completed = service.run_until_blocked()
    terminal = next(
        event for event in service.store.events.read()
        if event["event_type"] == "stage_completed"
    )
    authority = Path(service.paths.root) / terminal["payload"]["authority_path"]
    authority.unlink()

    with pytest.raises(ProductionError) as caught:
        service.get_status()
    assert caught.value.code == ReasonCode.STAGE_INTEGRITY_FAILED.value
    assert service.doctor()["status"] == "failed"


def test_completed_stage_checkpoint_deletion_invalidates_status(tmp_path):
    project_file, _ = _initialize(tmp_path)
    service = _service(project_file, RecordingRunner())
    with patch("manju.production.service.get_ai_config", return_value=(None, "mock-model", None)):
        service.run_until_blocked()
    terminal = next(
        event for event in service.store.events.read()
        if event["event_type"] == "stage_completed"
    )
    checkpoint_item = next(
        item for item in terminal["payload"]["authority_files"]
        if item["path"].endswith("checkpoints.sqlite")
    )
    (Path(service.paths.root) / checkpoint_item["path"]).unlink()

    with pytest.raises(ProductionError) as caught:
        service.get_status()
    assert caught.value.code == ReasonCode.STAGE_INTEGRITY_FAILED.value


def test_completed_storyboard_artifact_tamper_invalidates_status(tmp_path):
    project_file, _ = _initialize(tmp_path)
    service = _service(project_file, RecordingRunner())
    with patch("manju.production.service.get_ai_config", return_value=(None, "mock-model", None)):
        service.run_until_blocked()
    terminal = next(
        event for event in service.store.events.read()
        if event["event_type"] == "stage_completed"
    )
    artifact = Path(service.paths.root) / terminal["payload"]["artifacts"][0]["path"]
    artifact.write_text('{"tampered":true}', encoding="utf-8")

    with pytest.raises(ProductionError) as caught:
        service.get_status()
    assert caught.value.code == ReasonCode.STAGE_INTEGRITY_FAILED.value


def test_foreign_child_run_is_rejected_without_model_call(tmp_path):
    project_file, _ = _initialize(tmp_path)
    runner = RecordingRunner()
    service = _service(project_file, runner)
    with patch("manju.production.service.get_ai_config", return_value=(None, "mock-model", None)):
        snapshot = service.advance()
    invocation_id = f"storyboard-{snapshot.run_id.removeprefix('run_')}"
    output_dir = service.paths.storyboard_dir(snapshot.run_id, invocation_id)
    _write_stage_result(
        output_dir,
        source_path=service.store.source_path(service.store.load_project()),
        model="foreign-model",
    )

    with patch("manju.production.service.get_ai_config", return_value=(None, "mock-model", None)):
        failed = service.run_until_blocked()

    assert failed.status == "failed"
    assert failed.reason.code == ReasonCode.STAGE_INTEGRITY_FAILED.value
    assert runner.calls == []


def test_self_consistent_manifest_with_fake_checkpoint_is_rejected(tmp_path):
    project_file, _ = _initialize(tmp_path)
    runner = RecordingRunner()
    service = _service(project_file, runner)
    with patch("manju.production.service.get_ai_config", return_value=(None, "mock-model", None)):
        snapshot = service.advance()
    invocation_id = f"storyboard-{snapshot.run_id.removeprefix('run_')}"
    output_dir = service.paths.storyboard_dir(snapshot.run_id, invocation_id)
    _write_stage_result(
        output_dir,
        source_path=service.store.source_path(service.store.load_project()),
        valid_checkpoint=False,
    )

    with patch("manju.production.service.get_ai_config", return_value=(None, "mock-model", None)):
        failed = service.run_until_blocked()

    assert failed.status == "failed"
    assert failed.reason.code == ReasonCode.STAGE_INTEGRITY_FAILED.value
    assert runner.calls == []


def test_checkpoint_thread_must_match_manifest_run_id(tmp_path):
    project_file, _ = _initialize(tmp_path)
    runner = RecordingRunner()
    service = _service(project_file, runner)
    with patch("manju.production.service.get_ai_config", return_value=(None, "mock-model", None)):
        snapshot = service.advance()
    invocation_id = f"storyboard-{snapshot.run_id.removeprefix('run_')}"
    output_dir = service.paths.storyboard_dir(snapshot.run_id, invocation_id)
    _write_stage_result(
        output_dir,
        source_path=service.store.source_path(service.store.load_project()),
        checkpoint_thread_id="another-child-run",
    )

    with patch("manju.production.service.get_ai_config", return_value=(None, "mock-model", None)):
        failed = service.run_until_blocked()

    assert failed.status == "failed"
    assert failed.reason.code == ReasonCode.STAGE_INTEGRITY_FAILED.value
    assert runner.calls == []


def test_execution_lease_is_audited_and_balanced(tmp_path):
    project_file, _ = _initialize(tmp_path)
    service = _service(project_file, RecordingRunner())
    with patch("manju.production.service.get_ai_config", return_value=(None, "mock-model", None)):
        service.run_until_blocked()

    lease_events = [
        event for event in service.store.events.read()
        if event["event_type"].startswith("execution_lease_")
    ]
    assert lease_events
    assert [event["event_type"] for event in lease_events].count("execution_lease_acquired") == \
        [event["event_type"] for event in lease_events].count("execution_lease_released")
    acquired = {
        event["payload"]["lease_id"] for event in lease_events
        if event["event_type"] == "execution_lease_acquired"
    }
    released = {
        event["payload"]["lease_id"] for event in lease_events
        if event["event_type"] == "execution_lease_released"
    }
    assert acquired == released


def test_stale_execution_lease_recovery_is_audited(tmp_path):
    project_file, initialized = _initialize(tmp_path)
    service = _service(project_file, RecordingRunner())
    stale_id = "stale-lease"
    service.store.events.append(
        "execution_lease_acquired",
        project_id=initialized.project_id,
        payload={"lease_id": stale_id, "pid": 2147483647, "created_at": "2026-01-01T00:00:00Z"},
    )
    Path(service.paths.execution_lock_file).write_text(
        json.dumps({
            "lease_id": stale_id,
            "pid": 2147483647,
            "created_at": "2026-01-01T00:00:00Z",
        }),
        encoding="ascii",
    )

    with patch("manju.production.service.get_ai_config", return_value=(None, "mock-model", None)):
        service.advance()

    recovered = [
        event for event in service.store.events.read()
        if event["event_type"] == "execution_lease_recovered"
    ]
    assert recovered[-1]["payload"]["recovered_lease_id"] == stale_id


def test_top_event_binds_real_storyboard_agent_run_id(tmp_path):
    project_file, _ = _initialize(tmp_path)
    service = _service(project_file, RecordingRunner())
    with patch("manju.production.service.get_ai_config", return_value=(None, "mock-model", None)):
        service.run_until_blocked()
    terminal = next(
        event for event in service.store.events.read()
        if event["event_type"] == "stage_completed"
    )
    assert terminal["payload"]["stage_run_id"] == "child-run"
    attached = next(
        event for event in service.store.events.read()
        if event["event_type"] == "stage_run_attached"
    )
    assert attached["payload"]["stage_run_id"] == "child-run"


def test_cli_init_status_run_and_json_contract(tmp_path):
    source = _source(tmp_path)
    project_dir = tmp_path / "cli-project"
    runner = CliRunner()
    result = runner.invoke(cli, [
        "project", "init", "--source", str(source), "--source-type", "script",
        "-o", str(project_dir), "--json",
    ])
    assert result.exit_code == 0, result.output
    initialized = json.loads(result.output)
    assert initialized["status"] == "ready"

    status = runner.invoke(cli, ["status", str(project_dir / "project.json"), "--json"])
    assert status.exit_code == 0, status.output
    assert json.loads(status.output)["reason"]["code"] == ReasonCode.PROJECT_READY.value

    def completed_result(**kwargs):
        project = json.loads((project_dir / "project.json").read_text(encoding="utf-8"))
        _write_stage_result(
            kwargs["output_dir"], "completed",
            source_path=str(project_dir / project["source"]["path"]),
        )
        return StoryboardStageAdapter().inspect(
            stage_run_id=kwargs["stage_run_id"],
            output_dir=kwargs["output_dir"],
            expected=kwargs["expected"],
        )

    with patch("manju.production.service.get_ai_config", return_value=(None, "mock-model", None)), \
         patch("manju.production.adapters.storyboard.StoryboardStageAdapter.execute", side_effect=completed_result):
        executed = runner.invoke(cli, ["run", str(project_dir / "project.json"), "--json"])
    assert executed.exit_code == 0, executed.output
    assert json.loads(executed.output)["status"] == "completed"


def test_cli_json_is_not_polluted_by_storyboard_runner_output(tmp_path):
    source = _source(tmp_path)
    project_dir = tmp_path / "json-project"
    initialize_project(
        source=str(source), source_type="script", output_dir=str(project_dir), max_scenes=1
    )

    def noisy_runner(source_path, **kwargs):
        print("legacy progress that must not enter JSON")
        return _write_stage_result(
            kwargs["output_dir"], "completed", source_path=source_path,
            engine=kwargs["engine"],
            settings={
                "max_steps": kwargs["agent_max_steps"],
                "max_calls": kwargs["agent_max_calls"],
                "max_revisions": kwargs["agent_max_revisions"],
            },
        )

    runner = CliRunner()
    with patch("manju.production.service.get_ai_config", return_value=(None, "mock-model", None)), \
         patch("manju.production.adapters.storyboard.run_storyboard", side_effect=noisy_runner):
        result = runner.invoke(cli, ["run", str(project_dir / "project.json"), "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["status"] == "completed"
    assert "legacy progress" not in result.output


def test_cli_malformed_project_returns_structured_error(tmp_path):
    project_file, _ = _initialize(tmp_path)
    project = json.loads(project_file.read_text(encoding="utf-8"))
    project.pop("production")
    atomic_write_json(str(project_file), project)

    result = CliRunner().invoke(cli, ["run", str(project_file), "--json"])

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["error"]["code"] == ReasonCode.UNSUPPORTED_SCHEMA_VERSION.value
    assert "KeyError" not in result.output


def test_event_authority_hash_matches_recorded_manifest(tmp_path):
    project_file, _ = _initialize(tmp_path)
    runner = RecordingRunner()
    service = _service(project_file, runner)
    with patch("manju.production.service.get_ai_config", return_value=(None, "mock-model", None)):
        service.run_until_blocked()

    completed = next(
        event for event in service.store.events.read()
        if event["event_type"] == "stage_completed"
    )
    authority = Path(service.paths.root) / completed["payload"]["authority_path"]
    assert completed["payload"]["authority_hash"] == sha256_file(str(authority))


@pytest.mark.parametrize("engine", ["legacy", "workflow"])
def test_non_agent_storyboard_engines_complete_without_agent_manifest(tmp_path, engine):
    source = _source(tmp_path)
    project_dir = tmp_path / f"project-{engine}"
    initialize_project(
        source=str(source),
        source_type="script",
        output_dir=str(project_dir),
        engine=engine,
        max_scenes=1,
    )

    def runner(source_path, **kwargs):
        os.makedirs(kwargs["output_dir"], exist_ok=True)
        storyboard = {
            "schema_version": "2",
            "metadata": {
                "generation_engine": engine,
                "source_sha256": storyboard_source_sha256(source_path),
            },
            "scenes": [],
        }
        atomic_write_json(os.path.join(kwargs["output_dir"], "storyboard.json"), storyboard)
        return storyboard

    service = ProductionService(
        str(project_dir / "project.json"),
        storyboard_adapter=StoryboardStageAdapter(runner=runner),
    )
    with patch("manju.production.service.get_ai_config", return_value=(None, "mock-model", None)):
        completed = service.run_until_blocked()
    assert completed.status == "completed"
