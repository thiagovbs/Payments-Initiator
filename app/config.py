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

    # Endereço desta Iniciadora onde a detentora avisa mudanças de status do
    # consentimento. É o que fecha a jornada quando o titular aprova e fecha o
    # navegador sem voltar pelo /callback.
    #
    # O padrão é vazio de propósito, e não um localhost plausível: a URL precisa
    # estar na allow-list da detentora (WEBHOOK_ALLOWED_ORIGINS), e enviar uma
    # que ela não aceita faz a **criação do consentimento** falhar com 400 — ou
    # seja, um default errado derrubaria a jornada inteira em vez de só deixar
    # de avisar. Vazio = a detentora não é instruída a avisar, e a notícia chega
    # pela reconciliação do GET /payments/{identifier}.
    #
    # Use a mesma origem do CALLBACK_URL, com o caminho /webhooks/consents.
    webhook_url: str = ""

    # Allow-list de destinos para onde o /callback pode redirecionar o navegador
    # do titular ao fim do enrollment (o redirect_uri que o lojista informa em
    # POST /enrollments). Lista de origens separadas por vírgula, ex.:
    # "https://sebo-frontend.vercel.app,http://localhost:5173". Vazio = nenhum
    # redirect ao cliente é permitido (o /callback mantém a resposta JSON).
    enrollment_redirect_allowlist: str = ""

    organisation_id: str = ""
    authorisation_server_id: str = ""

    # Banco local. Fica num subdiretorio proprio para que o volume do Docker
    # persista apenas os dados, sem montar por cima do codigo da aplicacao.
    database_url: str = "sqlite:///./data/initiator.db"

    port: int = 8100

    # Autenticação dos usuários desta Iniciadora (JWT emitido em /auth/login).
    # Sem JWT_SECRET no ambiente a aplicação sobe com um segredo de
    # desenvolvimento e avisa no log: qualquer um que conheça o valor padrão
    # forjaria tokens válidos, então em produção ele é obrigatório.
    jwt_secret: str = "dev-insecure-jwt-secret-change-me"
    jwt_algorithm: str = "HS256"
    jwt_expires_minutes: int = 60

    # Usuários pré-cadastrados, semeados no start. Aceita
    # "alice:senha,bruno:outra" ou um JSON [{"username":..,"password":..}].
    initiator_users: str = ""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
