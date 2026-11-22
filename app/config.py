from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SANCTURAI_")

    database_url: str = "postgresql+asyncpg://sancturai:sancturai@localhost:5432/sancturai"
    internal_token: str = ""
    data_path: str = "/data"
    disk_busy_ratio: float = 0.85
    maintenance_interval_seconds: float = 60.0  # 0 = désactivé (tests)
    session_ttl_seconds: int = 86_400
    vault_quota_bytes: int = 50 * 1024 * 1024
    vault_file_max_bytes: int = 5 * 1024 * 1024
    vault_max_files: int = 10_000  # borne lignes (fichiers vides) et coût de vault_list
    post_max_bytes: int = 32 * 1024
    argon2_concurrency: int = 2
    allowed_hosts: list[str] = ["sancturai.bluegenji-esport.fr", "127.0.0.1:8300", "localhost:8300"]
    public_url: str = "https://sancturai.bluegenji-esport.fr"


@lru_cache
def get_settings() -> Settings:
    return Settings()
