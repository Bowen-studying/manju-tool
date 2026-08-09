"""Versioned, provider-neutral production knowledge used by Manju agents."""

from manju.knowledge.production_playbook import (
    PLAYBOOK_VERSION,
    get_playbook_sections,
    load_production_playbook,
)

__all__ = ["PLAYBOOK_VERSION", "get_playbook_sections", "load_production_playbook"]
