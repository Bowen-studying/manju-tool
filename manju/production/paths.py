"""Project-owned path layout."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class ProjectPaths:
    root: str

    @classmethod
    def from_project_file(cls, project_file: str) -> "ProjectPaths":
        return cls(os.path.dirname(os.path.abspath(project_file)))

    @property
    def project_file(self) -> str:
        return os.path.join(self.root, "project.json")

    @property
    def production_dir(self) -> str:
        return os.path.join(self.root, "production")

    @property
    def events_file(self) -> str:
        return os.path.join(self.production_dir, "events.jsonl")

    @property
    def state_file(self) -> str:
        return os.path.join(self.production_dir, "state.json")

    @property
    def artifacts_file(self) -> str:
        return os.path.join(self.production_dir, "artifacts.json")

    @property
    def revisions_file(self) -> str:
        return os.path.join(self.production_dir, "revisions.json")

    @property
    def lock_file(self) -> str:
        return os.path.join(self.production_dir, ".project.lock")

    @property
    def execution_lock_file(self) -> str:
        return os.path.join(self.production_dir, ".execution.lease")

    @property
    def sources_dir(self) -> str:
        return os.path.join(self.root, "sources")

    @property
    def outputs_dir(self) -> str:
        return os.path.join(self.root, "outputs")

    def run_dir(self, run_id: str) -> str:
        return os.path.join(self.production_dir, "runs", run_id)

    def contract_file(self, run_id: str) -> str:
        return os.path.join(self.run_dir(run_id), "contract.json")

    def storyboard_dir(self, run_id: str, stage_run_id: str) -> str:
        return os.path.join(self.run_dir(run_id), "stages", "storyboard", stage_run_id)

    def visual_dir(self, run_id: str, stage_run_id: str) -> str:
        return os.path.join(self.run_dir(run_id), "stages", "visual", stage_run_id)

    def voice_script_dir(self, run_id: str, stage_run_id: str) -> str:
        return os.path.join(self.run_dir(run_id), "stages", "voice_script", stage_run_id)

    def voice_director_dir(self, run_id: str, stage_run_id: str) -> str:
        return os.path.join(self.run_dir(run_id), "stages", "voice_director", stage_run_id)

    def voice_tts_dir(self, run_id: str, stage_run_id: str) -> str:
        return os.path.join(self.run_dir(run_id), "stages", "voice_tts", stage_run_id)

    def video_prompt_dir(self, run_id: str, stage_run_id: str) -> str:
        return os.path.join(self.run_dir(run_id), "stages", "video_prompt", stage_run_id)

    def video_dir(self, run_id: str, stage_run_id: str) -> str:
        return os.path.join(self.run_dir(run_id), "stages", "video", stage_run_id)

    @property
    def voice_director_policy_path(self) -> str:
        return os.path.join(self.production_dir, "policies", "voice_director_policy.json")

    @property
    def manual_dispatches_dir(self) -> str:
        return os.path.join(self.production_dir, "manual", "dispatches")

    @property
    def manual_results_dir(self) -> str:
        return os.path.join(self.production_dir, "manual", "results")

    def ensure_layout(self) -> None:
        for path in (self.production_dir, self.sources_dir, self.outputs_dir):
            os.makedirs(path, exist_ok=True)
