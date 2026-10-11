from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class SpikeSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SPIKE_", env_file=None, extra="forbid")
    database_url: str
    owner_id: int = Field(gt=0)
    chat_id: int
    timezone: str = "Asia/Yekaterinburg"
    mcp_secret: SecretStr = Field(min_length=32)
    transport_secret: SecretStr = Field(min_length=32)
    binding_secret: SecretStr = Field(min_length=32)
    binding_verified: bool = False
    binding_evidence: str = ""
    telegram_bot_token: SecretStr = SecretStr("")
    ai_api_key: SecretStr = SecretStr("")
    ai_folder_id: str = ""

    @field_validator("database_url")
    @classmethod
    def disposable_database(cls, value):
        if not urlsplit(value).path.endswith("_spike_test"):
            raise ValueError("spike requires an isolated database with _spike_test suffix")
        return value

    @field_validator("timezone")
    @classmethod
    def timezone_exists(cls, value):
        ZoneInfo(value)
        return value

    @model_validator(mode="after")
    def separate_authority(self):
        secrets = {
            self.mcp_secret.get_secret_value(),
            self.transport_secret.get_secret_value(),
            self.binding_secret.get_secret_value(),
        }
        if len(secrets) != 3:
            raise ValueError("MCP, transport and binding credentials must be different")
        if self.binding_verified and not self.binding_evidence.strip():
            raise ValueError("verified binding requires an evidence reference")
        return self
