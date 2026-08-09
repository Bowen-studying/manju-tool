"""Local confidence calibration for evidence-bearing vision verdicts.

Calibration is deliberately offline: callers provide human-labelled samples
or a previously generated report.  No provider credentials or API calls are
needed to build or apply a profile.
"""

from __future__ import annotations

import json
import math
from pathlib import Path


CALIBRATION_SCHEMA_VERSION = 1
DEFAULT_MINIMUM_SAMPLES = 50


def _sample_values(sample: dict) -> tuple[float, bool]:
    raw = sample.get("confidence", sample.get("predicted_confidence"))
    if not isinstance(raw, (int, float)) or isinstance(raw, bool):
        raise ValueError("calibration sample confidence must be a number")
    confidence = float(raw)
    if not math.isfinite(confidence) or not 0 <= confidence <= 1:
        raise ValueError("calibration sample confidence must be between 0 and 1")
    correct = sample.get("correct")
    if not isinstance(correct, bool):
        raise ValueError("calibration sample correct must be boolean")
    return confidence, correct


def calibration_report(
    samples: list[dict],
    *,
    bins: int = 10,
    minimum_samples: int = DEFAULT_MINIMUM_SAMPLES,
    provider_id: str = "",
    model: str = "",
) -> dict:
    """Return a deterministic reliability report and expected calibration error."""
    if not isinstance(bins, int) or not 2 <= bins <= 100:
        raise ValueError("calibration bins must be between 2 and 100")
    if not isinstance(minimum_samples, int) or minimum_samples < 1:
        raise ValueError("minimum_samples must be positive")
    values = [_sample_values(item) for item in samples if isinstance(item, dict)]
    buckets: list[list[tuple[float, bool]]] = [[] for _ in range(bins)]
    for confidence, correct in values:
        index = min(bins - 1, int(confidence * bins))
        buckets[index].append((confidence, correct))
    bin_reports: list[dict] = []
    ece = 0.0
    count = len(values)
    for index, bucket in enumerate(buckets):
        lower = index / bins
        upper = (index + 1) / bins
        mean_confidence = (
            sum(item[0] for item in bucket) / len(bucket) if bucket else None
        )
        accuracy = (
            sum(1 for _, correct in bucket if correct) / len(bucket) if bucket else None
        )
        if bucket and count:
            ece += (len(bucket) / count) * abs(float(mean_confidence) - float(accuracy))
        bin_reports.append({
            "index": index,
            "lower_inclusive": lower,
            "upper_inclusive": upper if index == bins - 1 else None,
            "upper_exclusive": None if index == bins - 1 else upper,
            "sample_count": len(bucket),
            "mean_confidence": mean_confidence,
            "observed_accuracy": accuracy,
        })
    return {
        "schema_version": CALIBRATION_SCHEMA_VERSION,
        "status": "calibrated" if count >= minimum_samples else "uncalibrated",
        "provider_id": str(provider_id),
        "model": str(model),
        "sample_count": count,
        "minimum_samples": minimum_samples,
        "bin_count": bins,
        "expected_calibration_error": ece if count else None,
        "bins": bin_reports,
    }


def effective_confidence(raw_confidence: float, profile: dict | None) -> float:
    """Map a raw score to observed accuracy only for a sufficiently sampled profile."""
    raw = max(0.0, min(1.0, float(raw_confidence)))
    if not isinstance(profile, dict) or profile.get("status") != "calibrated":
        return raw
    for bucket in profile.get("bins", []):
        if not isinstance(bucket, dict) or not bucket.get("sample_count"):
            continue
        lower = float(bucket.get("lower_inclusive", 0) or 0)
        upper_value = bucket.get("upper_exclusive")
        upper = float(upper_value) if upper_value is not None else 1.0
        if lower <= raw < upper or (raw == 1.0 and upper == 1.0):
            accuracy = bucket.get("observed_accuracy")
            if isinstance(accuracy, (int, float)) and not isinstance(accuracy, bool):
                return max(0.0, min(1.0, float(accuracy)))
    return raw


def calibrate_verdict(verdict: dict, profile: dict | None) -> dict:
    result = dict(verdict)
    raw = float(result.get("confidence", 0) or 0)
    effective = effective_confidence(raw, profile)
    result.update({
        "raw_confidence": raw,
        "confidence": effective,
        "calibration_status": (
            str(profile.get("status", "uncalibrated"))
            if isinstance(profile, dict) else "not_configured"
        ),
        "calibration_applied": bool(
            isinstance(profile, dict) and profile.get("status") == "calibrated"
        ),
    })
    return result


def calibration_summary(profile: dict | None) -> dict:
    if not isinstance(profile, dict) or not profile:
        return {
            "status": "not_configured",
            "sample_count": 0,
            "minimum_samples": DEFAULT_MINIMUM_SAMPLES,
            "calibration_applied": False,
        }
    return {
        key: profile.get(key)
        for key in (
            "schema_version", "status", "provider_id", "model", "sample_count",
            "minimum_samples", "bin_count", "expected_calibration_error",
        )
    } | {"calibration_applied": profile.get("status") == "calibrated"}


def load_calibration_profile(path: str) -> dict:
    """Load either labelled samples or a generated calibration report."""
    with Path(path).open(encoding="utf-8") as handle:
        payload = json.load(handle)
    if isinstance(payload, list):
        return calibration_report(payload)
    if not isinstance(payload, dict):
        raise ValueError("vision calibration file must contain an object or sample list")
    if isinstance(payload.get("samples"), list):
        return calibration_report(
            payload["samples"],
            bins=int(payload.get("bins", 10)),
            minimum_samples=int(
                payload.get("minimum_samples", DEFAULT_MINIMUM_SAMPLES)
            ),
            provider_id=str(payload.get("provider_id", "")),
            model=str(payload.get("model", "")),
        )
    required = {"status", "sample_count", "minimum_samples", "bins"}
    if not required.issubset(payload):
        raise ValueError("vision calibration report is missing required fields")
    if payload.get("status") not in {"calibrated", "uncalibrated"}:
        raise ValueError("vision calibration status must be calibrated or uncalibrated")
    return payload
