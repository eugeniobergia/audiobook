from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings, read from environment variables (or a local .env file)."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # No defaults on purpose: the app refuses to start without these.
    api_key: str
    postgres_user: str
    postgres_password: str
    postgres_db: str
    postgres_host: str = "localhost"
    postgres_port: int = 5432

    # Where the API server listens. Use 0.0.0.0 inside a container.
    host: str = "localhost"
    port: int = 8000

    # Upstream source. Point the URL somewhere unreachable to exercise the fallback.
    itunes_base_url: str = "https://itunes.apple.com"
    itunes_timeout_seconds: float = 10.0

    @property
    def database_url(self) -> str:
        return (
            f"postgresql://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
