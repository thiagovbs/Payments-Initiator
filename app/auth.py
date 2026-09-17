"""Autenticação dos usuários da Iniciadora.

A Iniciadora deixou de ser de um usuário só: cada chamada precisa dizer *quem*
está pagando, porque é o titular autenticado que amarra consentimentos e
dispositivos vinculados. O caminho é o comum — ``POST /auth/login`` com usuário
e senha pré-cadastrados devolve um JWT, e as demais rotas exigem esse token no
header ``Authorization: Bearer ...``.

Os usuários são semeados a partir de ``INITIATOR_USERS`` no start: não há
cadastro aberto, porque quem opera a Iniciadora é uma lista conhecida de
titulares, não qualquer um que alcance a porta 8100.
"""

import json
import logging

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

from .config import settings
from .security import TokenError, create_access_token, decode_access_token
from .security import hash_password, verify_password
from .store import UserRecord, count_users, get_user, upsert_user

logger = logging.getLogger(__name__)

router = APIRouter(tags=["auth"])

# ``auto_error=False`` para responder 401 com a mensagem desta aplicação (e o
# header WWW-Authenticate) em vez do 403 "Not authenticated" do FastAPI.
_bearer = HTTPBearer(auto_error=False, description="JWT obtido em POST /auth/login")


# ---------------------------------------------------------------------------
# Usuários pré-cadastrados
# ---------------------------------------------------------------------------


def parse_seed_users(raw: str) -> list[dict]:
    """Lê ``INITIATOR_USERS`` em JSON ou em pares ``usuario:senha``.

    JSON aceita ``full_name``; a forma curta existe para não precisar escapar
    um objeto inteiro dentro do ``.env``.
    """
    raw = (raw or "").strip()
    if not raw:
        return []

    if raw.startswith("["):
        try:
            entries = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("INITIATOR_USERS parece JSON mas não pôde ser lido")
            return []
        return [
            {
                "username": str(entry.get("username", "")).strip(),
                "password": str(entry.get("password", "")),
                "full_name": str(entry.get("full_name", "")).strip(),
            }
            for entry in entries
            if isinstance(entry, dict) and entry.get("username") and entry.get("password")
        ]

    users: list[dict] = []
    for pair in raw.split(","):
        username, separator, password = pair.partition(":")
        if not separator or not username.strip() or not password:
            continue
        users.append(
            {"username": username.strip(), "password": password, "full_name": ""}
        )
    return users


def seed_users() -> int:
    """Grava os usuários do ambiente no banco. Devolve quantos foram semeados.

    A senha é regravada a cada start: trocar o valor em ``INITIATOR_USERS``
    passa a valer sem precisar apagar o banco.
    """
    seeded = 0
    for entry in parse_seed_users(settings.initiator_users):
        upsert_user(
            UserRecord(
                username=entry["username"],
                password_hash=hash_password(entry["password"]),
                full_name=entry["full_name"],
            )
        )
        seeded += 1

    if seeded:
        logger.info("Usuários pré-cadastrados semeados: %s", seeded)
    elif count_users() == 0:
        logger.warning(
            "Nenhum usuário cadastrado: defina INITIATOR_USERS "
            "(ex.: 'alice:senha,bruno:outra') ou ninguém conseguirá autenticar."
        )

    if settings.jwt_secret == "dev-insecure-jwt-secret-change-me":
        logger.warning(
            "JWT_SECRET não definido: usando o segredo de desenvolvimento. "
            "Qualquer um que conheça esse valor forja tokens válidos."
        )
    elif len(settings.jwt_secret) < 32:
        # Abaixo disso o segredo entra no alcance de força bruta sobre os
        # tokens que a própria aplicação distribui (RFC 7518, seção 3.2).
        logger.warning(
            "JWT_SECRET tem menos de 32 caracteres; gere um maior "
            "(ex.: python -c \"import secrets; print(secrets.token_urlsafe(48))\")."
        )
    return seeded


def authenticate_user(username: str, password: str) -> UserRecord | None:
    """Confere usuário e senha. Devolve ``None`` em qualquer recusa."""
    user = get_user(username)
    if not user or user.disabled:
        return None
    if not verify_password(password, user.password_hash):
        return None
    return user


# ---------------------------------------------------------------------------
# Dependência de rota
# ---------------------------------------------------------------------------


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


def current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> UserRecord:
    """Resolve o usuário do ``Bearer`` token; 401 se ele não se sustentar.

    O token só identifica: o usuário é relido do banco a cada chamada, então
    desabilitar alguém tem efeito imediato, sem esperar o token expirar.
    """
    if credentials is None or not credentials.credentials:
        raise _unauthorized("Token de acesso ausente")

    try:
        claims = decode_access_token(credentials.credentials)
    except TokenError as exc:
        raise _unauthorized(str(exc)) from exc

    user = get_user(claims.get("sub", ""))
    if not user or user.disabled:
        raise _unauthorized("Usuário do token não está mais ativo")
    return user


# ---------------------------------------------------------------------------
# Rotas
# ---------------------------------------------------------------------------


class LoginRequest(BaseModel):
    username: str = Field(..., description="Usuário pré-cadastrado na Iniciadora")
    password: str = Field(..., description="Senha do usuário")


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int = Field(..., description="Validade do token, em segundos")
    expires_at: str
    username: str


@router.post("/auth/login", response_model=TokenResponse)
def login(req: LoginRequest) -> TokenResponse:
    """Troca usuário e senha por um access token JWT."""
    user = authenticate_user(req.username, req.password)
    if not user:
        # Mesma resposta para usuário inexistente e senha errada: a diferença
        # entre as duas diria a um atacante quais usuários existem.
        raise _unauthorized("Usuário ou senha inválidos")

    token = create_access_token(user.username)
    return TokenResponse(**token, username=user.username)


class MeResponse(BaseModel):
    username: str
    full_name: str


@router.get("/auth/me", response_model=MeResponse)
def me(user: UserRecord = Depends(current_user)) -> MeResponse:
    """Devolve o usuário do token — útil para conferir se ele ainda vale."""
    return MeResponse(username=user.username, full_name=user.full_name)
