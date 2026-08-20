"""Optional enhancements. Every one is off by default and degrades silently.

Nothing in this package is required for the fleet to work. Each module is
gated behind its own environment variable and falls back to the core behaviour
when unavailable, so a missing credential or a slow API can never take a run
down.

  ARMCL_GEMMA_TRIAGE=true    Gemma-assisted salience classification
  ARMCL_VEO_BRIEFING=true    Veo-generated incident briefings
"""

from app.bonus.gemma_triage import triage
from app.bonus.veo_briefing import generate_briefing

__all__ = ["generate_briefing", "triage"]
