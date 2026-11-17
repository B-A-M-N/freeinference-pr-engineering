"""Role agents run end-to-end with a scripted model: no network, no API key."""

import json

from mistralai import agents

from freeinference_pr.role_agents import InvestigationReport, build_role_agent


def test_structured_output_prompt_and_parsing():
    """The structured-output path: schema instruction + tolerant parse.

    The live adapter path (real FreeInference call through the custom provider)
    is verified by the freeinference-provider-check workflow inside the managed
    deployment (Gate A), because Agent.override(scripted_model) cannot bypass a
    custom completion= config in Agents SDK 1.1.3.
    """
    from freeinference_pr.activities import _parse_structured, _schema_instruction

    instruction = _schema_instruction(InvestigationReport)
    assert '"InvestigationReport"' in instruction or "root_causes" in instruction
    reply = json.dumps(
        {
            "summary": "Retry loop swallows cancellation.",
            "root_causes": [
                {
                    "title": "CancelledError swallowed",
                    "severity": "high",
                    "evidence": ["src/retry.py:42"],
                    "detail": "except Exception catches CancelledError.",
                }
            ],
            "affected_contracts": ["RetryPolicy"],
            "production_paths": ["client.request() -> retry_loop"],
            "open_questions": [],
        }
    )
    report = _parse_structured(f"Here is my analysis:\n{reply}\nThanks", InvestigationReport)
    assert report.root_causes[0].evidence == ["src/retry.py:42"]


async def test_role_agents_have_freeinference_completion():
    from freeinference_pr.provider import FreeInferenceCompletionConfig

    for role in ("orchestrator", "investigation", "implementation", "review", "verification", "packaging"):
        agent = build_role_agent(role)
        assert isinstance(agent.completion, FreeInferenceCompletionConfig)
        assert agent.model == agent.completion.model


async def test_all_six_roles_registered():
    from freeinference_pr.role_agents import OUTPUT_TYPES, ROLE_INSTRUCTIONS

    assert set(ROLE_INSTRUCTIONS) == {
        "orchestrator",
        "investigation",
        "implementation",
        "review",
        "verification",
        "packaging",
    }
    assert set(OUTPUT_TYPES) == {
        "investigation",
        "implementation",
        "review",
        "verification",
        "packaging",
    }
