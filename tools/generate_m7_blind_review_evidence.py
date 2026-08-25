#!/usr/bin/env python3
"""Build machine-readable M7 blind-review evidence from private source files."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path


DIMENSIONS = ("source_fidelity", "shot_continuity", "shootability", "character_consistency")
SCORE_RE = re.compile(r"^(\d+)\s*/\s*(\d+)$")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _source_record(path: Path, role: str) -> dict[str, str]:
    return {"role": role, "path": path.name, "sha256": _sha256(path)}


def _engine(value: str) -> str:
    value = value.replace("*", "").strip()
    if "Agent" in value:
        return "agent"
    if "冻结" in value or "workflow" in value.lower():
        return "workflow"
    raise ValueError(f"unrecognised engine in mapping: {value!r}")


def _parse_mapping(path: Path) -> dict[str, dict[str, str]]:
    mapping: dict[str, dict[str, str]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        fields = [part.strip() for part in line.strip().strip("|").split("|")]
        if len(fields) != 4 or not re.fullmatch(r"\d{2}", fields[0]):
            continue
        group, a_value, b_value, agent_side = fields
        if agent_side not in {"A", "B"}:
            continue
        mapping[group] = {
            "A": _engine(a_value),
            "B": _engine(b_value),
            "agent_side": agent_side,
            "workflow_side": "B" if agent_side == "A" else "A",
        }
    if len(mapping) != 19:
        raise ValueError(f"expected 19 mapping rows, found {len(mapping)}")
    return dict(sorted(mapping.items()))


def _parse_reviewer(path: Path) -> dict[str, dict]:
    result: dict[str, dict] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        fields = [part.strip() for part in line.strip().strip("|").split("|")]
        if len(fields) < 8 or not fields[0].startswith("组"):
            continue
        group = fields[0].removeprefix("组")
        if not re.fullmatch(r"\d{2}", group):
            continue
        scores = []
        for field in fields[1:5]:
            match = SCORE_RE.fullmatch(field)
            if not match:
                raise ValueError(f"invalid score in {path.name}: {field!r}")
            scores.append([int(match.group(1)), int(match.group(2))])
        preference = fields[5]
        if preference not in {"A", "B", "平局"}:
            raise ValueError(f"invalid preference in {path.name}: {preference!r}")
        result[group] = {
            "scores": dict(zip(DIMENSIONS, scores)),
            "preference": preference,
            "serious_error": {"A": fields[6].startswith("是"), "B": fields[7].startswith("是")},
        }
    if len(result) != 19:
        raise ValueError(f"expected 19 score rows in {path.name}, found {len(result)}")
    return dict(sorted(result.items()))


def _aggregate(rows: list[int]) -> dict[str, float | int]:
    total = sum(rows)
    return {"sum": total, "count": len(rows), "mean": round(total / len(rows), 6) if rows else None}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mapping-file", required=True, type=Path)
    parser.add_argument("--review-file", action="append", required=True, type=Path,
                        help="重复三次，按评委文件传入")
    parser.add_argument("--summary-file", type=Path, default=None,
                        help="可选的人工汇总文件，仅记录 hash，不作为计算来源")
    parser.add_argument("--mapping-output", type=Path, required=True)
    parser.add_argument("--summary-output", type=Path, required=True)
    args = parser.parse_args()
    if len(args.review_file) != 3:
        parser.error("--review-file must be supplied exactly three times")

    mapping = _parse_mapping(args.mapping_file)
    reviewers = [_parse_reviewer(path) for path in args.review_file]
    groups = set(mapping)
    if any(set(scores) != groups for scores in reviewers):
        raise ValueError("mapping and reviewer groups do not match")

    source_files = [_source_record(args.mapping_file, "mapping")]
    source_files.extend(_source_record(path, f"reviewer_{index}") for index, path in enumerate(args.review_file, 1))
    if args.summary_file:
        source_files.append(_source_record(args.summary_file, "human_summary_reference"))

    side_scores = {side: {dimension: [] for dimension in DIMENSIONS} for side in ("A", "B")}
    engine_scores = {engine: {dimension: [] for dimension in DIMENSIONS} for engine in ("agent", "workflow")}
    winner_groups = {"agent": [], "workflow": [], "tie": []}
    serious_groups = {"agent": [], "workflow": []}
    group_results = {}

    for group in sorted(groups):
        rows = [reviewer[group] for reviewer in reviewers]
        preference_votes = {side: sum(row["preference"] == side for row in rows) for side in ("A", "B")}
        preference_votes["tie"] = sum(row["preference"] == "平局" for row in rows)
        if preference_votes["A"] > preference_votes["B"] and preference_votes["A"] > preference_votes["tie"]:
            winner_side = "A"
        elif preference_votes["B"] > preference_votes["A"] and preference_votes["B"] > preference_votes["tie"]:
            winner_side = "B"
        else:
            winner_side = "tie"
        winner_engine = None if winner_side == "tie" else mapping[group][winner_side]
        winner_groups[winner_engine or "tie"].append(group)

        serious_votes = {side: sum(row["serious_error"][side] for row in rows) for side in ("A", "B")}
        serious_majority = {side: serious_votes[side] >= 2 for side in ("A", "B")}
        for side, is_majority in serious_majority.items():
            if is_majority:
                serious_groups[mapping[group][side]].append(group)

        for dimension in DIMENSIONS:
            for side in ("A", "B"):
                values = [row["scores"][dimension][0 if side == "A" else 1] for row in rows]
                side_scores[side][dimension].extend(values)
                engine_scores[mapping[group][side]][dimension].extend(values)

        group_results[group] = {
            "mapping": mapping[group],
            "winner_side": winner_side,
            "winner_engine": winner_engine,
            "preference_votes": preference_votes,
            "serious_error_votes": serious_votes,
            "serious_error_majority": serious_majority,
        }

    def aggregate_scope(scope: dict[str, dict[str, list[int]]]) -> dict:
        return {name: {dimension: _aggregate(values) for dimension, values in dimensions.items()}
                for name, dimensions in scope.items()}

    def aggregate_combined(scope: dict[str, dict[str, list[int]]]) -> dict:
        return {name: _aggregate([value for dimension in DIMENSIONS for value in dimensions[dimension]])
                for name, dimensions in scope.items()}

    source_hashes = {record["role"]: record["sha256"] for record in source_files}
    anonymous_side_wins = {
        side: sum(result["winner_side"] == side for result in group_results.values())
        for side in ("A", "B", "tie")
    }
    mapping_doc = {
        "schema_version": "m7-blind-review-mapping-v1",
        "group_count": len(groups),
        "seed": None,
        "mapping": mapping,
        "source_files": source_files,
        "source_file_hashes": source_hashes,
        "anonymous_side_preference_wins": anonymous_side_wins,
        "note": "The source mapping file does not record the random seed; A/B preference counts are not decoded engine results.",
    }
    summary_doc = {
        "schema_version": "m7-blind-review-summary-v1",
        "group_count": len(groups),
        "reviewer_count": len(reviewers),
        "scores_per_dimension": len(groups) * len(reviewers),
        "seed": None,
        "mapping": mapping,
        "winner_groups": winner_groups,
        "winner_counts": {name: len(groups_for_name) for name, groups_for_name in winner_groups.items()},
        "agent_win_rate": len(winner_groups["agent"]) / len(groups),
        "serious_error_majority_groups": serious_groups,
        "serious_error_majority_counts": {name: len(groups_for_name) for name, groups_for_name in serious_groups.items()},
        "scores_by_anonymous_side": aggregate_scope(side_scores),
        "scores_by_decoded_engine": aggregate_scope(engine_scores),
        "combined_scores_by_anonymous_side": aggregate_combined(side_scores),
        "combined_scores_by_decoded_engine": aggregate_combined(engine_scores),
        "groups": group_results,
        "source_files": source_files,
        "source_file_hashes": source_hashes,
        "note": "A10/B9 is the original anonymous-side summary; decoded winners are computed from the mapping and raw reviewer rows.",
    }
    for output, document in ((args.mapping_output, mapping_doc), (args.summary_output, summary_doc)):
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
