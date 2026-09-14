from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_environment: str = "development"
    public_base_url: str = ""
    database_url: str = "sqlite:///./socioai.db"
    evolution_base_url: str = "http://localhost:8080"
    evolution_api_key: str = ""
    evolution_instance: str = ""
    evolution_webhook_secret: str = ""
    llm_provider: str = "deterministic"
    llm_base_url: str = "https://api.openai.com/v1"
    llm_api_key: str = ""
    llm_model: str = "gpt-4.1-mini"
    data_dir: str = "./data"
    max_messages_per_day: int = 200
    transcription_base_url: str = "https://api.openai.com/v1"
    transcription_api_key: str = ""
    transcription_model: str = "whisper-1"
    require_onboarding: bool = True
    terms_version: str = "2026-09"
    privacy_version: str = "2026-09"
    admin_api_key: str = ""
    payment_provider: str = "generic"
    payment_webhook_secret: str = ""
    recommendation_confidence: float = 0.75
    recommendation_cooldown_days: int = 30

    @property
    def is_deployed(self) -> bool:
        return self.app_environment.casefold() in {"local", "staging", "production"}

    def validate_runtime(self) -> None:
        """Reject test fallbacks and incomplete credentials in deployed environments."""
        if not self.is_deployed:
            return
        required = {
            "DATABASE_URL": self.database_url if self.database_url.startswith("postgresql") else "",
            "EVOLUTION_BASE_URL": self.evolution_base_url,
            "EVOLUTION_API_KEY": self.evolution_api_key,
            "EVOLUTION_INSTANCE": self.evolution_instance,
            "EVOLUTION_WEBHOOK_SECRET": self.evolution_webhook_secret,
            "LLM_BASE_URL": self.llm_base_url,
            "LLM_API_KEY": self.llm_api_key,
            "LLM_MODEL": self.llm_model,
            "TRANSCRIPTION_BASE_URL": self.transcription_base_url,
            "TRANSCRIPTION_API_KEY": self.transcription_api_key,
            "TRANSCRIPTION_MODEL": self.transcription_model,
            "ADMIN_API_KEY": self.admin_api_key,
        }
        missing = [name for name, value in required.items() if not str(value).strip()]
        if self.llm_provider != "openai-compatible":
            missing.append("LLM_PROVIDER=openai-compatible")
        if self.app_environment.casefold() in {"staging", "production"} and not self.public_base_url.startswith("https://"):
            missing.append("PUBLIC_BASE_URL=https://...")
        if missing:
            raise RuntimeError(
                "Invalid real-runtime configuration; set required credentials: "
                + ", ".join(sorted(set(missing)))
            )


@lru_cache
def get_settings() -> Settings:
    return Settings()
