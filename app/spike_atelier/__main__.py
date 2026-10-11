import uvicorn

from app.config import Settings
from app.db import make_sessions
from app.providers import YandexProvider
from app.spike_atelier.api import create_app
from app.spike_atelier.config import SpikeSettings
from app.spike_atelier.service import SpikeService


def application():
    config = SpikeSettings()
    sessions = make_sessions(config.database_url)
    # Pass every default explicitly; production environment variables cannot fill gaps.
    provider_config = Settings(
        _env_file=None,
        **{
            name: field.default
            for name, field in Settings.model_fields.items()
            if name
            not in {
                "database_url",
                "ai_api_key",
                "ai_folder_id",
            }
        },
        database_url=config.database_url,
        ai_api_key=config.ai_api_key,
        ai_folder_id=config.ai_folder_id,
    )
    service = SpikeService(sessions, YandexProvider(provider_config, sessions), config)
    return create_app(service)


if __name__ == "__main__":
    uvicorn.run(application(), host="127.0.0.1", port=8011, access_log=False)
