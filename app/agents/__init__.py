"""The fleet's agents.

Six run inside the workflow graph. The intake partner and explainer run outside
it, before and after autonomous case processing respectively.
"""

from app.agents.analyst import analyst_agent
from app.agents.approver import approver_agent
from app.agents.critic import critic_agent
from app.agents.evolver import evolver_agent
from app.agents.executor import executor_agent
from app.agents.explainer import explainer_agent
from app.agents.intake_partner import intake_partner_agent
from app.agents.opportunity_partner import opportunity_partner_agent
from app.agents.scout import scout_agent

__all__ = [
    "analyst_agent",
    "approver_agent",
    "critic_agent",
    "evolver_agent",
    "executor_agent",
    "explainer_agent",
    "intake_partner_agent",
    "opportunity_partner_agent",
    "scout_agent",
]
