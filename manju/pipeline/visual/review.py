"""Stable, evidence-bearing visual review verdicts."""

from __future__ import annotations

from dataclasses import dataclass

from manju.utils.runtime import content_fingerprint


ALLOWED_VERDICTS = frozenset({"pass", "fail", "unverifiable"})


@dataclass(frozen=True)
class ConstraintVerdict:
    constraint_id: str
    verdict: str
    evidence: tuple[dict, ...]
    confidence: float
    reviewer: str
    measurement: dict

    def __post_init__(self) -> None:
        if self.verdict not in ALLOWED_VERDICTS:
            raise ValueError(f"unsupported constraint verdict: {self.verdict}")
        if not 0 <= self.confidence <= 1:
            raise ValueError("constraint verdict confidence must be between 0 and 1")
        if self.verdict == "fail" and not self.evidence:
            raise ValueError("a failing constraint verdict requires evidence")

    def to_dict(self) -> dict:
        return {
            "constraint_id": self.constraint_id,
            "verdict": self.verdict,
            "evidence": [dict(item) for item in self.evidence],
            "confidence": self.confidence,
            "reviewer": self.reviewer,
            "measurement": dict(self.measurement),
        }


def normalize_issue_verdict(issue: dict) -> dict:
    evidence_valid = issue.get("evidence_valid") is True
    constraint_id = str(issue.get("constraint_id", "")) or str(
        issue.get("correction_contract_id", "")
    )
    if not constraint_id:
        constraint_id = "legacy_" + content_fingerprint(
            issue.get("group_id", ""), issue.get("shot_id", ""),
            issue.get("correction_target", ""), issue.get("focus_asset_ids", []),
            length=20,
        )
    raw_confidence = issue.get("confidence")
    confidence = float(raw_confidence) if isinstance(raw_confidence, (int, float)) else (
        1.0 if evidence_valid else 0.0
    )
    evidence = issue.get("evidence")
    if not isinstance(evidence, list):
        evidence = [{
            "image_path": str(issue.get("image_path", "")),
            "problem": str(issue.get("problem", "")),
            "legacy_derived": True,
        }] if evidence_valid else []
    verdict = "fail" if issue.get("blocking") and evidence else "unverifiable"
    return {
        "protocol_version": "4.0",
        "constraint_id": constraint_id,
        "verdict": verdict,
        "evidence": evidence,
        "confidence": max(0.0, min(1.0, confidence)),
        "measurement": dict(issue.get("measurement", {}))
        if isinstance(issue.get("measurement"), dict) else {},
    }


def blocking_verdict_is_actionable(verdict: dict, minimum_confidence: float = 0.75) -> bool:
    return bool(
        verdict.get("verdict") == "fail"
        and isinstance(verdict.get("evidence"), list)
        and verdict["evidence"]
        and float(verdict.get("confidence", 0) or 0) >= minimum_confidence
    )
