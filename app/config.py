from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    database_url: str = "postgresql+psycopg://assistant:assistant@localhost:5432/assistant"
    service_token: SecretStr = SecretStr("")
    telegram_bot_token: SecretStr = SecretStr("")
    allowed_telegram_user_id: int = 0
    timezone: str = "Europe/Moscow"
    backend_url: str = "http://backend:8000"
    ai_api_key: SecretStr = SecretStr("")
    ai_base_url: str = "https://ai.api.cloud.yandex.net/v1"
    ai_folder_id: str = ""
    generation_model: str = "deepseek-v4-flash"
    embedding_doc_model: str = "text-embeddings-v2-doc/"
    embedding_query_model: str = "text-embeddings-v2-query/"
    tool_protocol: str = "native"
    pricing_path: Path = Path("config/pricing.json")
    file_directory: Path = Path("data/documents")
    ai_timeout_seconds: int = Field(default=45, ge=1, le=90)
    ai_attempts: int = Field(default=2, ge=1, le=3)

    @field_validator("timezone")
    @classmethod
    def timezone_exists(cls, value: str) -> str:
        ZoneInfo(value)
        return value

    @field_validator("tool_protocol")
    @classmethod
    def protocol_exists(cls, value: str) -> str:
        if value not in {"native", "json"}:
            raise ValueError("tool_protocol must be native or json")
        return value

    def model_uri(self, model: str, embedding: bool = False) -> str:
        # The live OpenAI embeddings gateway rejects an empty version segment;
        # make the documented default version explicit without changing the model.
        if embedding and model.endswith("/"):
            model += "latest"
        if model.startswith(("gpt://", "emb://")):
            return model
        return f"{'emb' if embedding else 'gpt'}://{self.ai_folder_id}/{model}"


@lru_cache
def settings() -> Settings:
    return Settings()
