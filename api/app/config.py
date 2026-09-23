from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

_ENV_FILE = Path(__file__).resolve().parent.parent / '.env'


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=_ENV_FILE,
        env_file_encoding='utf-8',
        extra='ignore',
    )

    app_name: str = 'Signals API'
    database_url: str = 'postgresql+asyncpg://signals:password@127.0.0.1:5432/signals'
    cors_origins: str = 'http://localhost:4200'


@lru_cache
def get_settings() -> Settings:
    return Settings()
