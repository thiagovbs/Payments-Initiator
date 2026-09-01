"""Configuração central da Iniciadora."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Carrega as variáveis de ambiente (com suporte a .env)."""

    # Credenciais da iniciadora (usadas para o token no gateway em produção)
    initiator_client_id: str = ""
    initiator_client_secret: str = ""

    # Em desenvolvimento: URL do core-banking (chamada direta, paths /v1/...)
    core_base_url: str = "http://localhost:3000"
    # Em produção: URL do gateway proxy Sensedia (paths /open-banking/...)
    gateway_base_url: str = ""
    # True = usa o gateway (produção) | False = chama o core direto (dev)
    use_proxy: bool = False

    callback_url: str = "http://localhost:8100/callback"

    # Paths formais Open Finance (usados em produção via gateway)
    pisp_path: str = "open-banking/pisp"
    aspsp_path: str = "open-banking/journey-aspsp"

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
