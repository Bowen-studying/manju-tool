"""Approval templates and field-addressable validation errors."""

from __future__ import annotations

from dataclasses import dataclass
import re
import unicodedata


@dataclass(frozen=True)
class ValidationIssue:
    path: str
    message: str
    example: object = None

    def to_dict(self) -> dict:
        return {"path": self.path, "message": self.message, "example": self.example}


class DecisionValidationError(ValueError):
    def __init__(self, issues: list[ValidationIssue]):
        self.issues = issues
        summary = "; ".join(f"{item.path}: {item.message}" for item in issues)
        super().__init__(summary)


_PLACEHOLDER_VALUES = {
    "", "auto", "automatic", "automated", "ok", "okay", "yes", "approved", "pass",
    "scriptselected", "scriptapproved", "通过", "同意", "自动审批", "自动选择", "脚本选择", "脚本审批",
}


def is_placeholder_review_text(value: object) -> bool:
    normalized = unicodedata.normalize("NFKC", str(value or "")).casefold()
    compact = re.sub(r"[^\w\u4e00-\u9fff]+", "", normalized)
    return compact in _PLACEHOLDER_VALUES or any(compact.startswith(prefix) for prefix in (
        "auto", "automatic", "automated", "scriptselected", "scriptapproved",
        "自动审批", "自动选择", "脚本选择", "脚本审批", "已审阅所有图片", "reviewedallimages",
    ))


def decision_template(request: dict) -> dict:
    issues = request.get("issues", []) if isinstance(request.get("issues"), list) else []
    return {
        "request_id": str(request.get("request_id", "")),
        "state_fingerprint": str(request.get("state_fingerprint", "")),
        "decision": "pending",
        "selections": {},
        "override_reason": "",
        "change_note": "",
        "reviewer": "",
        "reviewed_item_ids": list(request.get("item_ids", [])),
        "reviewed_image_fingerprints": dict(request.get("reviewed_image_fingerprints", {})),
        "issue_override_reasons": {
            str(issue.get("issue_id")): ""
            for issue in issues
            if isinstance(issue, dict) and issue.get("blocking") is True
            and str(issue.get("issue_id", ""))
        },
        "reference_contract_checks": {},
    }


def validate_common_decision(request: dict, decision: dict, choice: str) -> None:
    issues: list[ValidationIssue] = []
    reviewer = str(decision.get("reviewer", "")).strip()
    if is_placeholder_review_text(reviewer) or len(reviewer) < 2:
        issues.append(ValidationIssue(
            "$.reviewer", "requires a non-placeholder reviewer identity", "Human Reviewer"
        ))
    expected_items = {str(item) for item in request.get("item_ids", [])}
    reviewed = decision.get("reviewed_item_ids")
    if not isinstance(reviewed, list) or {str(item) for item in reviewed} != expected_items:
        issues.append(ValidationIssue(
            "$.reviewed_item_ids", "must exactly match request.item_ids",
            list(request.get("item_ids", [])),
        ))
    expected_fingerprints = request.get("reviewed_image_fingerprints", {})
    if expected_fingerprints and decision.get("reviewed_image_fingerprints") != expected_fingerprints:
        issues.append(ValidationIssue(
            "$.reviewed_image_fingerprints",
            "must exactly match the request; use the prefilled decision template",
            expected_fingerprints,
        ))
    if choice in {"reject", "regenerate"}:
        note = str(decision.get("change_note") or decision.get("override_reason") or "").strip()
        if is_placeholder_review_text(note) or len(note) < 8:
            issues.append(ValidationIssue(
                "$.change_note", "reject/regenerate requires a specific note of at least 8 characters",
                "Describe the observed issue and requested action.",
            ))
    allowed = [str(value).lower() for value in request.get("allowed_decisions", [])]
    if allowed and choice not in allowed:
        issues.append(ValidationIssue(
            "$.decision", f"decision {choice!r} is not allowed; must be one of {allowed}", allowed[0],
        ))
    if issues:
        raise DecisionValidationError(issues)
