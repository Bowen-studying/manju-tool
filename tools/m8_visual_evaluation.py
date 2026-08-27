#!/usr/bin/env python3
"""CLI for the offline M8 visual-quality contract and evidence helpers.

This command only freezes local manifests, packages already-existing image
pairs, or aggregates human score files.  It never imports a Provider client or
starts a network request.  Formal packaging reads the externally managed
ProductionRun HMAC key only to verify signed evidence and never persists it.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from manju.pipeline.visual.evaluation import (  # noqa: E402
    M8EvaluationError,
    aggregate_reviews,
    attest_pair_evidence,
    build_sample_manifest,
    freeze_m8,
    generate_blind_materials,
    validate_sample_manifest,
)


def _path(value: str) -> Path:
    return Path(value).expanduser()


def _print_result(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Freeze or summarize M8 visual evidence without calling a Provider."
    )
    commands = parser.add_subparsers(dest="command", required=True)

    freeze = commands.add_parser("freeze", help="freeze the source-bound contract and sample scope")
    freeze.add_argument(
        "--source-manifest", type=_path,
        default=ROOT / "m7_samples" / "manifest.json",
    )
    freeze.add_argument(
        "--contract-output", type=_path,
        default=ROOT / "m8_evaluation" / "contract.json",
    )
    freeze.add_argument(
        "--sample-output", type=_path,
        default=ROOT / "m8_evaluation" / "samples.json",
    )
    freeze.add_argument("--baseline-commit", default="d7191cd")
    freeze.add_argument("--baseline-branch", default="feat/m3.4.1-audit-baseline")

    validate = commands.add_parser("validate-sample", help="validate a frozen M8 sample manifest")
    validate.add_argument("--sample-manifest", type=_path, required=True)

    attest = commands.add_parser(
        "attest-pair",
        help="append the trusted final HMAC witness to one private pair evidence file",
    )
    attest.add_argument("--evidence-file", type=_path, required=True)

    package = commands.add_parser("generate-ab", help="package local A/B images for blind review")
    package.add_argument("--pairs-file", type=_path, required=True)
    package.add_argument("--public-dir", type=_path, required=True)
    package.add_argument("--mapping-output", type=_path, required=True)
    package.add_argument("--sample-manifest", type=_path)
    package.add_argument("--seed", type=int)

    aggregate = commands.add_parser("aggregate", help="validate and aggregate three blind reviews")
    aggregate.add_argument("--materials-manifest", type=_path, required=True)
    aggregate.add_argument("--mapping-file", type=_path, required=True)
    aggregate.add_argument("--review-file", type=_path, action="append", required=True)
    aggregate.add_argument("--output", type=_path, required=True)
    aggregate.add_argument("--contract", type=_path, required=True)
    aggregate.add_argument("--sample-manifest", type=_path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        if args.command == "freeze":
            result = freeze_m8(
                source_manifest_path=args.source_manifest,
                contract_output=args.contract_output,
                sample_output=args.sample_output,
                baseline_commit=args.baseline_commit,
                baseline_branch=args.baseline_branch,
            )
        elif args.command == "validate-sample":
            manifest_path = args.sample_manifest.resolve()
            with manifest_path.open("r", encoding="utf-8") as handle:
                result = validate_sample_manifest(json.load(handle))
        elif args.command == "attest-pair":
            result = attest_pair_evidence(args.evidence_file)
        elif args.command == "generate-ab":
            result = generate_blind_materials(
                pair_input_path=args.pairs_file,
                public_output_dir=args.public_dir,
                private_mapping_output=args.mapping_output,
                sample_manifest_path=args.sample_manifest,
                seed=args.seed,
            )
        else:
            result = aggregate_reviews(
                materials_manifest_path=args.materials_manifest,
                private_mapping_path=args.mapping_file,
                review_paths=args.review_file,
                output_path=args.output,
                contract_path=args.contract,
                sample_manifest_path=args.sample_manifest,
            )
        _print_result(result)
        return 0
    except (M8EvaluationError, OSError, UnicodeError, TypeError, ValueError) as exc:
        print(f"M8 evaluation error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
