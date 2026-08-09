"""Load the curated production playbook without exposing private source notes."""

from __future__ import annotations

import json
import os
from functools import lru_cache
from typing import Iterable


PLAYBOOK_VERSION = "1.0"


@lru_cache(maxsize=1)
def load_production_playbook() -> dict:
    path = os.path.join(os.path.dirname(__file__), "production_playbook.json")
    with open(path, encoding="utf-8") as handle:
        value = json.load(handle)
    if value.get("version") != PLAYBOOK_VERSION:
        raise ValueError("production playbook version mismatch")
    return value


def get_playbook_sections(tags: Iterable[str], levels: Iterable[str] | None = None) -> list[dict]:
    """Return only the sections relevant to the requested agent action."""
    wanted_tags = {str(item).strip() for item in tags if str(item).strip()}
    wanted_levels = set(levels or ("hard_gate", "advisory"))
    result: list[dict] = []
    for section in load_production_playbook().get("sections", []):
        if section.get("level") not in wanted_levels:
            continue
        if wanted_tags.intersection(section.get("tags", [])):
            result.append({
                "section_id": section["section_id"],
                "level": section["level"],
                "rules": list(section.get("rules", [])),
            })
    return result
