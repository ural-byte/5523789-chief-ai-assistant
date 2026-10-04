"""Composition root. Adapters and domain hooks are wired only at application startup."""

from app.domain_actions import CreateTask, PrepareMeeting, handle_callback
from app.tools import registry


def install(sessions, provider=None, config=None):
    if "create_task" not in registry.tools:
        registry.register("create_task", CreateTask())
        registry.register("prepare_meeting", PrepareMeeting())

    async def callback(job, lease):
        await handle_callback(sessions, job, lease)

    registry.callback = callback
