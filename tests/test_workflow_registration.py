"""Workflow definitions register with the SDK and expose their handlers."""

import mistralai.workflows as workflows

from freeinference_pr.pr_workflow import PREngineeringAssistantWorkflow
from freeinference_pr.provider_check import ProviderCheckWorkflow


def test_mission_workflow_registers():
    definition = workflows.get_workflow_definition(PREngineeringAssistantWorkflow)
    assert definition.name == "pr-engineering-assistant"
    handler_names = {h.name for h in definition.queries} if hasattr(definition, "queries") else set()
    assert "mission_state" in handler_names
    assert "mission_key" in handler_names


def test_provider_check_workflow_registers():
    definition = workflows.get_workflow_definition(ProviderCheckWorkflow)
    assert definition.name == "freeinference-provider-check"
