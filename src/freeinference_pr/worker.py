"""Managed worker entrypoint: register all workflow classes and serve them."""

from __future__ import annotations

import asyncio
import signal

import mistralai.workflows as workflows

import freeinference_pr.pr_workflow  # noqa: F401 - imported for workflow registration
import freeinference_pr.provider_check  # noqa: F401


def _workflow_classes() -> list[type]:
    from freeinference_pr.pr_workflow import PREngineeringAssistantWorkflow
    from freeinference_pr.provider_check import ProviderCheckWorkflow

    return [PREngineeringAssistantWorkflow, ProviderCheckWorkflow]


async def main() -> None:
    workflow_classes = _workflow_classes()
    names = [workflows.get_workflow_definition(c).name for c in workflow_classes]
    served = asyncio.current_task()
    stopped_by: signal.Signals | None = None

    def _stop(stop_signal: signal.Signals) -> None:
        nonlocal stopped_by
        if stopped_by is not None:
            return
        stopped_by = stop_signal
        if served is not None:
            served.cancel()

    loop = asyncio.get_running_loop()
    for stop_signal in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(stop_signal, _stop, stop_signal)
    try:
        await workflows.run_worker([*workflow_classes])
    except asyncio.CancelledError:
        if stopped_by is None:
            raise


if __name__ == "__main__":
    asyncio.run(main())
