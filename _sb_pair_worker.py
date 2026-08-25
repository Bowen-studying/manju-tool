#!/usr/bin/env python3
"""Single-engine storyboard worker for M7 blind review generation.

Runs run_storyboard for ONE engine with the injected LLM env, prints the
complete storyboard dict as the last JSON line on stdout.

Env: SB_INPUT, SB_OUTPUT, SB_ENGINE (agent|workflow), LLM_* or WORKFLOW_LLM_*.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from manju.pipeline.storyboard import run_storyboard


def main() -> int:
    src = os.environ["SB_INPUT"]
    outdir = os.environ["SB_OUTPUT"]
    engine = os.environ["SB_ENGINE"]
    if engine == "agent":
        os.environ["LLM_API_BASE"] = os.environ.get("LLM_API_BASE", "http://127.0.0.1:8788/v1")
        os.environ["LLM_API_KEY"] = os.environ.get("LLM_API_KEY", "dummy")
        os.environ["LLM_MODEL"] = os.environ.get("LLM_MODEL", "gpt-5.6-sol")
    else:
        os.environ["LLM_API_BASE"] = os.environ.get("WORKFLOW_LLM_BASE", "https://apihub.agnes-ai.com/v1")
        os.environ["LLM_API_KEY"] = os.environ.get("WORKFLOW_LLM_KEY", "")
        os.environ["LLM_MODEL"] = os.environ.get("WORKFLOW_LLM_MODEL", "agnes-2.0-flash")
    os.makedirs(outdir, exist_ok=True)
    try:
        result = run_storyboard(
            src, output_dir=os.path.join(outdir, engine),
            engine=engine, agent_max_revisions=1, agent_max_steps=20,
            image_api=False,
        )
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"engine": engine, "error": str(exc)}, ensure_ascii=False))
        return 1
    if not isinstance(result, dict):
        print(json.dumps({"engine": engine, "error": "no storyboard"}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
