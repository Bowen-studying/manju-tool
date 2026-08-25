#!/usr/bin/env python3
"""Finalise M7 blind-review materials from completed raw storyboards.

Reads raw/<id>/<engine>/storyboard.json pairs, anonymises (no engine/path/
timestamp), randomises A/B with a printed seed, writes reviewer-facing
documents under 匿名评审版/, and PRINTS the private mapping to stdout only.

Usage:
  python m7_blind_review_finalize.py [--dry-run]
"""
from __future__ import annotations

import json
import os
import random
import sys

ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
REVIEW_DIR = os.environ.get("MANJU_BLIND_REVIEW_DIR", os.path.join(ROOT_DIR, "m7_blind_review_materials"))
RAW_DIR = os.path.join(REVIEW_DIR, "raw")
ANON_DIR = os.path.join(REVIEW_DIR, "匿名评审版")
EXTRA_DIR = os.path.join(REVIEW_DIR, "extra")
SAMPLES_DIR = os.environ.get("MANJU_M7_SAMPLES_DIR", os.path.join(ROOT_DIR, "m7_samples"))

INPUTS = [
    "s01", "s02", "s03", "s05",
    "n01", "n02", "n03", "n04", "n05",
    "x01", "x02", "x03", "x04", "x05",
    "b01", "b02", "b03", "b04", "b05", "b06",
]


def _anonymise_storyboard(storyboard: dict) -> str:
    lines: list[str] = []
    for scene in storyboard.get("scenes", []):
        heading = scene.get("heading") or scene.get("scene_heading") or "场景"
        lines.append(f"【场景】{heading}")
        for shot in scene.get("shots", []):
            visual = shot.get("visual", {})
            desc = visual.get("description", "") if isinstance(visual, dict) else shot.get("visual_description", "")
            lines.append(f"  镜头: {desc}")
            audio = shot.get("audio", {}) if isinstance(shot.get("audio"), dict) else {}
            if audio.get("dialogue"):
                lines.append(f"    对白({audio.get('speaker','')}): {audio['dialogue']}")
            if audio.get("narration"):
                lines.append(f"    旁白: {audio['narration']}")
    return "\n".join(lines)


def _source_path(iid: str) -> str:
    if iid.startswith("b"):
        return os.path.join(EXTRA_DIR, f"{iid}_*.txt")
    return os.path.join(SAMPLES_DIR, f"{iid}_*.txt")


def _resolve_source(iid: str) -> str | None:
    import glob
    matches = glob.glob(_source_path(iid))
    return matches[0] if matches else None


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--review-dir", default=None)
    parser.add_argument("--raw-dir", default=None)
    parser.add_argument("--anon-dir", default=None)
    parser.add_argument("--extra-dir", default=None)
    parser.add_argument("--samples-dir", default=None)
    parser.add_argument("--mapping-output", default=None, help="可选：保存私有机器可读映射")
    parser.add_argument("--seed", type=int, default=None, help="可复现的 A/B 随机种子")
    args = parser.parse_args()

    global REVIEW_DIR, RAW_DIR, ANON_DIR, EXTRA_DIR, SAMPLES_DIR
    if args.review_dir:
        REVIEW_DIR = os.path.abspath(args.review_dir)
    RAW_DIR = os.path.abspath(args.raw_dir) if args.raw_dir else os.path.join(REVIEW_DIR, "raw")
    ANON_DIR = os.path.abspath(args.anon_dir) if args.anon_dir else os.path.join(REVIEW_DIR, "匿名评审版")
    EXTRA_DIR = os.path.abspath(args.extra_dir) if args.extra_dir else os.path.join(REVIEW_DIR, "extra")
    if args.samples_dir:
        SAMPLES_DIR = os.path.abspath(args.samples_dir)
    dry = args.dry_run
    pairs: list[tuple[str, dict, dict]] = []
    for iid in INPUTS:
        a = os.path.join(RAW_DIR, iid, "agent", "storyboard.json")
        w = os.path.join(RAW_DIR, iid, "workflow", "storyboard.json")
        if os.path.isfile(a) and os.path.isfile(w):
            with open(a, encoding="utf-8") as fh:
                agent = json.load(fh)
            with open(w, encoding="utf-8") as fh:
                workflow = json.load(fh)
            pairs.append((iid, agent, workflow))
        else:
            print(f"[missing] {iid} agent={os.path.isfile(a)} workflow={os.path.isfile(w)}", flush=True)

    if not pairs:
        print("no pairs")
        return 1

    seed = args.seed if args.seed is not None else int.from_bytes(os.urandom(4), "big")
    labelled: list[tuple[str, str, str, str, str]] = []
    mapping: dict[str, dict] = {"seed": seed, "pairs": {}}
    for _attempt in range(200):
        rng = random.Random(seed)
        mapping = {"seed": seed, "pairs": {}}
        labelled = []
        for idx, (iid, agent, workflow) in enumerate(pairs, start=1):
            label = f"组{idx:02d}"
            left = "agent" if rng.random() < 0.5 else "workflow"
            right = "workflow" if left == "agent" else "agent"
            mapping["pairs"][label] = {"input_id": iid, "A": left, "B": right}
            src = _resolve_source(iid) or ""
            labelled.append((label, iid, left, right, src))
        a_left = sum(1 for item in labelled if item[2] == "agent")
        # Balance the A/B left position roughly half/half (protocol requirement).
        if abs(a_left - len(labelled) / 2) <= 1:
            break
        seed = int.from_bytes(os.urandom(4), "big")
    print(f"[balance] A=agent left {sum(1 for item in labelled if item[2] == 'agent')}/{len(labelled)}", flush=True)

    os.makedirs(ANON_DIR, exist_ok=True)
    if not dry:
        for label, iid, left, right, src in labelled:
            with open(os.path.join(ANON_DIR, f"{label}.md"), "w", encoding="utf-8") as fh:
                fh.write(f"# {label}\n\n## 输入原文\n\n")
                if src:
                    with open(src, encoding="utf-8") as sfh:
                        fh.write(sfh.read().strip() + "\n\n")
                fh.write("## A\n\n")
                fh.write(_anonymise_storyboard(pairs[[p[0] for p in pairs].index(iid)][1 if left == "agent" else 2]) + "\n\n")
                fh.write("## B\n\n")
                fh.write(_anonymise_storyboard(pairs[[p[0] for p in pairs].index(iid)][1 if right == "agent" else 2]) + "\n")

    print("=== MAPPING (private) ===")
    print(json.dumps(mapping, ensure_ascii=False))
    mapping_output = args.mapping_output or os.environ.get("M7_BLIND_MAPPING_OUTPUT", "")
    if mapping_output and not dry:
        mapping_output = os.path.abspath(mapping_output)
        os.makedirs(os.path.dirname(mapping_output), exist_ok=True)
        with open(mapping_output, "w", encoding="utf-8") as handle:
            json.dump({"schema_version": "m7-blind-review-mapping-v1", **mapping}, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
    print(f"pairs={len(labelled)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
