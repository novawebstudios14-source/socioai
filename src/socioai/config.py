from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "sqlite:///./socioai.db"
    evolution_base_url: str = "http://localhost:8080"
    evolution_api_key: str = ""
    evolution_webhook_secret: str = ""
    llm_provider: str = "deterministic"
    llm_base_url: str = "https://api.openai.com/v1"
    llm_api_key: str = ""
    llm_model: str = "gpt-4.1-mini"


@lru_cache
def get_settings() -> Settings:
    return Settings()

