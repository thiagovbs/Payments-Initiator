"""Configuração central da Iniciadora."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Carrega as variáveis de ambiente (com suporte a .env)."""

    # Credenciais da iniciadora (usadas para o token no gateway em produção)
    initiator_client_id: str = ""
    initiator_client_secret: str = ""

    # URL do core-banking (chamada direta)
    core_base_url: str = "http://localhost:3000"

    callback_url: str = "http://localhost:8100/callback"

    organisation_id: str = ""
    authorisation_server_id: str = ""

    port: int = 8100

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
