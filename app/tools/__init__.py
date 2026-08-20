"""Agent-facing tools, bound to whichever domain is registered."""

from app.tools.fleet_tools import (
    act_on_item,
    discover_candidates,
    inspect_item,
    verify_action,
)
from app.tools.protocol import DomainAdapter, active_domain, register_domain

__all__ = [
    "DomainAdapter",
    "act_on_item",
    "active_domain",
    "discover_candidates",
    "inspect_item",
    "register_domain",
    "verify_action",
]
