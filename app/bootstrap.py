"""Composition root. Adapters and domain hooks are wired only at application startup."""

from app.document_download import download_document
from app.domain_actions import CreateTask, PrepareMeeting, handle_callback
from app.domain_memory import Documents, SaveMemory, SearchDocument, SearchMemory
from app.tools import registry


def install(sessions, provider=None, config=None):
    if "create_task" not in registry.tools:
        registry.register("create_task", CreateTask())
        registry.register("prepare_meeting", PrepareMeeting())

    async def callback(job, lease):
        await handle_callback(sessions, job, lease)

    registry.callback = callback
    if provider is not None and config is not None:
        for name, handler in {
            "save_memory": SaveMemory(sessions, provider),
            "search_memory": SearchMemory(sessions, provider),
            "search_document": SearchDocument(sessions, provider),
        }.items():
            registry.tools[name] = handler
        documents = Documents(sessions, provider, config)
        registry.upload = documents.upload
        registry.jobs["pdf_index"] = documents.index

        async def download(job, lease):
            await download_document(sessions, config, job, lease)

        registry.jobs["document"] = download
