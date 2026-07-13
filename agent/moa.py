"""Scaffold for a future Mixture-of-Agents implementation.

MOA is intentionally not wired into the runtime yet. The current project uses
delegate_task for practical multi-agent work; this module keeps the future MOA
shape discoverable without adding token cost or tool surface area.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Any


MOA_DISABLED_MESSAGE = (
    "MOA scaffold is present but disabled. Use delegate_task for current multi-agent work."
)


@dataclass(frozen=True)
class MoaAgentSpec:
    """One advisory agent slot in a future MOA round."""

    name: str
    role: str = ""
    model: str = ""


@dataclass(frozen=True)
class MoaRequest:
    """Future MOA request shape.

    The intended flow is: fan out the same question to advisory agents, collect
    their independent answers, then ask one aggregator to synthesize a final
    answer. This is not executed today.
    """

    question: str
    context: str = ""
    agents: tuple[MoaAgentSpec, ...] = ()
    aggregator_model: str = ""


def normalize_moa_request(
    question: str,
    context: str = "",
    agents: list[dict[str, Any]] | None = None,
    aggregator_model: str = "",
) -> MoaRequest:
    """Normalize user-facing MOA-like input into a stable internal shape."""
    normalized_agents: list[MoaAgentSpec] = []
    for index, agent in enumerate(agents or []):
        if not isinstance(agent, dict):
            raise ValueError(f"Agent {index} must be an object.")
        name = str(agent.get("name") or f"agent_{index + 1}").strip()
        normalized_agents.append(
            MoaAgentSpec(
                name=name,
                role=str(agent.get("role") or ""),
                model=str(agent.get("model") or ""),
            )
        )
    return MoaRequest(
        question=str(question or "").strip(),
        context=str(context or ""),
        agents=tuple(normalized_agents),
        aggregator_model=str(aggregator_model or ""),
    )


def run_moa_disabled(
    question: str,
    context: str = "",
    agents: list[dict[str, Any]] | None = None,
    aggregator_model: str = "",
) -> str:
    """Return the disabled MOA payload without calling any model."""
    request = normalize_moa_request(
        question=question,
        context=context,
        agents=agents,
        aggregator_model=aggregator_model,
    )
    payload = {
        "status": "disabled",
        "message": MOA_DISABLED_MESSAGE,
        "request": {
            **asdict(request),
            "agents": [asdict(agent) for agent in request.agents],
        },
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)
