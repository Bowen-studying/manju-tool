#!/usr/bin/env python3
"""M7 acceptance runner: 20 fixed samples x 6 scenarios.

Scenario matrix per sample:
  1. first run to terminal state (completed / needs_review / failed with reason)
  2. process-interrupt recovery (fresh service continues, no duplicate paid calls)
  3. same-config re-run (idempotent reuse, no rework of unrelated assets)
  4. artifact tamper (authority/artifact mutation fails closed)
  5. source revision (source change detected -> SOURCE_HASH_MISMATCH)
  6. re-import (legacy storyboard samples: repeated explicit import is idempotent)

Production stages use deterministic offline adapters (voice-tts mock, visual
mock, video-prompt offline) so the run is reproducible and free.  Storyboard
generation uses a deterministic fixture adapter: storyboard quality is judged
separately by the human blind review (M7 section 5).
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from manju.production.adapters.base import StageResult  # noqa: E402
from manju.production.models import ProductionError, ProductionStatus  # noqa: E402
from manju.production.security import MappingHmacKeyProvider  # noqa: E402
from manju.production.service import ProductionService, initialize_project  # noqa: E402
from manju.production.store import sha256_file  # noqa: E402
from tests.test_production_m4_0_voice_script import FixtureStoryboardAdapter  # noqa: E402

KEY = b"m7-acceptance-key"
ROOT = os.path.dirname(os.path.abspath(__file__))
SAMPLES_DIR = os.path.join(ROOT, "m7_samples")
MANIFEST_PATH = os.path.join(SAMPLES_DIR, "manifest.json")
EVIDENCE_DIR = os.path.join(ROOT, "m7_evidence")
WORK_ROOT = os.path.join(EVIDENCE_DIR, "work")


def _service(source_path: str, sample: dict, work_dir: str) -> tuple[ProductionService, str]:
    cfg = sample["enabled_config"]
    project = os.path.join(work_dir, "project")
    if os.path.exists(os.path.join(project, "project.json")):
        raise ProductionError("PROJECT_DIR_NONEMPTY", "target project already exists")
    kwargs: dict = {
        "source": source_path,
        "source_type": "storyboard" if sample["kind"] == "legacy_storyboard" else "script",
        "output_dir": project,
        "engine": "agent",
        "hmac_key_id": "m7-key",
    }
    if cfg.get("voice_script"):
        kwargs["voice_script_enabled"] = True
    if cfg.get("voice_director"):
        kwargs["voice_director_enabled"] = True
    if cfg.get("voice_tts"):
        kwargs["voice_tts_enabled"] = True
        kwargs["voice_tts_mode"] = "offline_mock"
    if cfg.get("video_prompt"):
        kwargs["video_prompt_enabled"] = True
    if cfg.get("visual"):
        kwargs["visual_enabled"] = True
        kwargs["visual_maximum_paid_calls"] = 1
        kwargs["visual_maximum_amount"] = str(sample["budget_ceiling_minor"])
        kwargs["visual_settlement_mode"] = "provider_evidence"
    initialize_project(**kwargs)
    service = ProductionService(
        os.path.join(project, "project.json"),
        storyboard_adapter=FixtureStoryboardAdapter(),
        hmac_key_provider=MappingHmacKeyProvider({"m7-key": KEY}),
    )
    service._configured_model = lambda: "fixture"
    return service, project


def _run_to_terminal(service: ProductionService, max_advances: int = 60) -> dict:
    snapshot = service.get_status()
    steps = 0
    for _ in range(max_advances):
        if snapshot.status in {
            ProductionStatus.COMPLETED.value, ProductionStatus.NEEDS_REVIEW.value,
            ProductionStatus.BLOCKED.value, ProductionStatus.FAILED.value,
            ProductionStatus.CANCELLED.value, ProductionStatus.SUPERSEDED.value,
            ProductionStatus.AWAITING_APPROVAL.value,
        }:
            break
        snapshot = service.advance()
        steps += 1
    events = service.store.events.read()
    reason = getattr(snapshot, "reason", None)
    if reason is not None and not isinstance(reason, (str, type(None))):
        reason = {"code": getattr(reason, "code", ""), "message": getattr(reason, "message", "")}
    return {
        "status": snapshot.status,
        "reason": reason,
        "last_event_hash": events[-1].get("hash", "") if events else "",
        "event_count": len(events),
        "steps": steps,
        "paid_calls": [e["event_type"] for e in events if e["event_type"].startswith("call_")],
    }


def scenario_first_run(source_path: str, sample: dict, work_dir: str) -> dict:
    service, _ = _service(source_path, sample, os.path.join(work_dir, "s1"))
    result = _run_to_terminal(service)
    result["events"] = service.store.events.read()
    return result


def scenario_interrupt_recovery(source_path: str, sample: dict, work_dir: str) -> dict:
    base = os.path.join(work_dir, "s2")
    service, _ = _service(source_path, sample, base)
    service.advance()  # start run
    service.advance()  # one more step (storyboard running)
    paid_before = [e["event_type"] for e in service.store.events.read() if e["event_type"].startswith("call_")]
    # "process restart": fresh service instance on the same project
    service2 = ProductionService(
        os.path.join(base, "project", "project.json"),
        storyboard_adapter=FixtureStoryboardAdapter(),
        hmac_key_provider=MappingHmacKeyProvider({"m7-key": KEY}),
    )
    service2._configured_model = lambda: "fixture"
    result = _run_to_terminal(service2)
    paid_after = [e["event_type"] for e in service2.store.events.read() if e["event_type"].startswith("call_")]
    result["paid_before_restart"] = paid_before
    result["paid_after_restart"] = paid_after
    result["no_duplicate_paid"] = paid_before == paid_after or not paid_before
    return result


def scenario_rerun(source_path: str, sample: dict, work_dir: str) -> dict:
    base = os.path.join(work_dir, "s3")
    service, _ = _service(source_path, sample, base)
    first = _run_to_terminal(service)
    event_count_first = first["event_count"]
    # fresh service on completed project must not redo work
    service2 = ProductionService(
        os.path.join(base, "project", "project.json"),
        storyboard_adapter=FixtureStoryboardAdapter(),
        hmac_key_provider=MappingHmacKeyProvider({"m7-key": KEY}),
    )
    service2._configured_model = lambda: "fixture"
    second = _run_to_terminal(service2)
    result = {"first": first, "second": second,
              "no_rework": second["event_count"] <= event_count_first + 2}
    return result


def scenario_tamper(source_path: str, sample: dict, work_dir: str) -> dict:
    base = os.path.join(work_dir, "s4")
    service, _ = _service(source_path, sample, base)
    _run_to_terminal(service)
    events = service.store.events.read()
    terminal = next(
        (e for e in reversed(events)
         if e["event_type"] in {"stage_completed", "stage_failed"}
         and (e.get("payload") or {}).get("stage") == "storyboard"),
        None,
    )
    if terminal is None or not terminal.get("payload", {}).get("authority_path"):
        return {"tamper_detected": False, "reason": "no storyboard terminal"}
    authority_path = service.store.artifact_path(terminal["payload"]["authority_path"])
    if os.path.isfile(authority_path):
        with open(authority_path, "rb") as handle:
            data = bytearray(handle.read())
        data[-1] ^= 0xFF
        with open(authority_path, "wb") as handle:
            handle.write(data)
    try:
        doc = service.doctor()
    except ProductionError as exc:
        return {"tamper_detected": True, "reason_code": exc.code}
    if doc.get("integrity_status") == "failed":
        return {"tamper_detected": True, "reason_code": (doc.get("checks") or [{}])[-1].get("code", "")}
    return {"tamper_detected": False, "reason": "doctor did not fail closed"}


def scenario_source_revision(source_path: str, sample: dict, work_dir: str) -> dict:
    base = os.path.join(work_dir, "s5")
    os.makedirs(base, exist_ok=True)
    # work copy so the shared sample file is never mutated
    work_source = os.path.join(base, "source.txt")
    shutil.copyfile(source_path, work_source)
    service, project = _service(work_source, sample, base)
    _run_to_terminal(service)
    # the project references its own copy under sources/; revise that copy
    project_data = json.loads(open(os.path.join(project, "project.json"), encoding="utf-8").read())
    inner_source = os.path.join(project, project_data["source"]["path"])
    if not os.path.isfile(inner_source):
        inner_source = work_source
    with open(inner_source, "a", encoding="utf-8") as handle:
        handle.write("\nREVISED")
    try:
        doc = service.doctor()
    except ProductionError as exc:
        return {"revision_detected": True, "reason_code": exc.code}
    if doc.get("integrity_status") == "failed":
        return {"revision_detected": True, "reason_code": (doc.get("checks") or [{}])[-1].get("code", "")}
    return {"revision_detected": False}


def scenario_reimport(source_path: str, sample: dict, work_dir: str) -> dict:
    from manju.production import import_legacy_storyboard
    base = os.path.join(work_dir, "s1")
    project = os.path.join(base, "project")
    first = import_legacy_storyboard(str(source_path), str(project), video_prompt_enabled=True)
    second = import_legacy_storyboard(str(source_path), str(project), video_prompt_enabled=True)
    idempotent = first.status == second.status
    # doctor then run to terminal; the imported project owns its storyboard
    # adapter version and model name, so continue with defaults (no custom
    # adapter, no model override) to match the frozen contract
    service = ProductionService(
        os.path.join(project, "project.json"),
        hmac_key_provider=MappingHmacKeyProvider({"manju-local-default": KEY}),
    )
    doctor = service.doctor()
    run = _run_to_terminal(service, max_advances=80)
    return {"first_status": first.status, "second_status": second.status,
            "idempotent": idempotent, "doctor_status": doctor.get("integrity_status"),
            "run_status": run["status"], "run_reason": run["reason"]}


def run_sample(sample: dict) -> dict:
    sample_id = sample["id"]
    kind = sample["kind"]
    work_dir = os.path.join(WORK_ROOT, sample_id)
    if os.path.isdir(work_dir):
        shutil.rmtree(work_dir)
    os.makedirs(work_dir, exist_ok=True)
    source_path = os.path.join(SAMPLES_DIR, sample["filename"])
    source_sha = hashlib.sha256(open(source_path, "rb").read()).hexdigest()
    results: dict = {"id": sample_id, "kind": kind, "source_sha256": source_sha}
    try:
        if kind == "legacy_storyboard":
            results["s1_first_run"] = scenario_reimport(source_path, sample, work_dir)
            results["s6_reimport"] = scenario_reimport(source_path, sample, work_dir)
        else:
            results["s1_first_run"] = scenario_first_run(source_path, sample, work_dir)
            results["s2_interrupt_recovery"] = scenario_interrupt_recovery(source_path, sample, work_dir)
            results["s3_rerun"] = scenario_rerun(source_path, sample, work_dir)
            results["s4_tamper"] = scenario_tamper(source_path, sample, work_dir)
            results["s5_source_revision"] = scenario_source_revision(source_path, sample, work_dir)
    except Exception as exc:  # noqa: BLE001
        results["error"] = f"{type(exc).__name__}: {exc}"
    return results


def main() -> int:
    manifest = json.loads(open(MANIFEST_PATH, encoding="utf-8").read())
    os.makedirs(EVIDENCE_DIR, exist_ok=True)
    all_results = []
    for sample in manifest["samples"]:
        print(f"running {sample['id']} ...", flush=True)
        all_results.append(run_sample(sample))
    evidence_path = os.path.join(EVIDENCE_DIR, "samples_results.json")
    with open(evidence_path, "w", encoding="utf-8") as handle:
        json.dump({"manifest": manifest, "results": all_results}, handle, ensure_ascii=False, indent=2)
    print(f"evidence written to {evidence_path}")
    failures = [r for r in all_results if "error" in r]
    print(f"completed {len(all_results)} samples, {len(failures)} errors")
    for r in failures:
        print(f"  ERROR {r['id']}: {r['error']}")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
