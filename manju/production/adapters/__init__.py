"""Stage adapters exposed to ProductionRun."""

from manju.production.adapters.base import StageResult
from manju.production.adapters.storyboard import StoryboardStageAdapter
from manju.production.adapters.visual import VisualStageAdapter
from manju.production.adapters.voice_script import VoiceScriptStageAdapter

__all__ = ["StageResult", "StoryboardStageAdapter", "VisualStageAdapter", "VoiceScriptStageAdapter"]
