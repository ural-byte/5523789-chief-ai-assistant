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
    from app.data_controls import PrepareDataDeletion, cleanup_deletion
    from app.memory_overview import show_memory
    from app.memory_resolution import PrepareMemoryResolution, prepare_job

    registry.tools["prepare_memory_resolution"] = PrepareMemoryResolution()

    async def resolution(job, lease):
        await prepare_job(sessions, job, lease)

    registry.jobs["memory_resolution"] = resolution

    registry.tools["prepare_data_deletion"] = PrepareDataDeletion(
        config.file_directory if config else None
    )

    async def overview(job, lease):
        await show_memory(sessions, job, lease)

    async def cleanup(job, lease):
        await cleanup_deletion(sessions, config, job, lease)

    registry.jobs["memory_overview"] = overview
    registry.jobs["data_cleanup"] = cleanup
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
