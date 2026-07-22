"""Application settings loaded from .env."""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Keep secrets outside source code."""

    groq_api_key: str = ""
    groq_model: str = ""
    jwt_secret_key: str = ""
    access_token_expire_minutes: int = 30
    rate_limit_salt: str = ""
    chat_requests_per_hour: int = 20
    search_requests_per_hour: int = 60
    uploads_per_hour: int = 5
    groq_calls_per_day: int = 50
    max_folder_files: int = 25
    max_file_size_mb: int = 25
    max_folder_total_size_mb: int = 200
    max_concurrent_file_processing: int = 3

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()
