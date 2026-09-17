"""Primitivas de senha e de token JWT da Iniciadora.

Duas responsabilidades, ambas sem I/O: transformar senha em hash verificável e
emitir/validar o access token que identifica o usuário nas chamadas seguintes.
Quem persiste usuários é ``store.py``; quem decide o que fazer com o token é
``auth.py``.
"""

import base64
import hashlib
import hmac
import secrets
import uuid
from datetime import datetime, timedelta, timezone

import jwt

from .config import settings

# PBKDF2-HMAC-SHA256: sem dependência nativa (bcrypt/argon2) e suficiente para
# senhas de demonstração. O custo fica gravado no próprio hash, então dá para
# subir as iterações depois sem invalidar os hashes já emitidos.
_ALGORITHM = "pbkdf2_sha256"
_ITERATIONS = 200_000
_SALT_BYTES = 16


def hash_password(password: str, *, iterations: int = _ITERATIONS) -> str:
    """Devolve ``pbkdf2_sha256$iteracoes$salt$hash`` (tudo em base64 urlsafe)."""
    salt = secrets.token_bytes(_SALT_BYTES)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return "$".join([_ALGORITHM, str(iterations), _b64(salt), _b64(digest)])


def verify_password(password: str, encoded: str) -> bool:
    """Confere a senha contra o hash, em tempo constante."""
    try:
        algorithm, raw_iterations, raw_salt, raw_digest = encoded.split("$")
        if algorithm != _ALGORITHM:
            return False
        expected = _unb64(raw_digest)
        digest = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), _unb64(raw_salt), int(raw_iterations)
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(digest, expected)


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _unb64(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


# ---------------------------------------------------------------------------
# Access token (JWT HS256)
# ---------------------------------------------------------------------------

ISSUER = "payment-initiator"


class TokenError(Exception):
    """Token ausente, expirado, adulterado ou emitido por outro serviço."""


def create_access_token(username: str, *, expires_minutes: int | None = None) -> dict:
    """Emite o access token do usuário e devolve também quando ele expira."""
    minutes = expires_minutes or settings.jwt_expires_minutes
    issued_at = datetime.now(timezone.utc)
    expires_at = issued_at + timedelta(minutes=minutes)
    token = jwt.encode(
        {
            "sub": username,
            "iss": ISSUER,
            "iat": issued_at,
            "exp": expires_at,
            "jti": uuid.uuid4().hex,
        },
        settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
    )
    return {
        "access_token": token,
        "token_type": "bearer",
        "expires_in": int(minutes * 60),
        "expires_at": expires_at.isoformat(),
    }


def decode_access_token(token: str) -> dict:
    """Valida assinatura, emissor e expiração; devolve as claims."""
    try:
        return jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=[settings.jwt_algorithm],
            issuer=ISSUER,
            options={"require": ["sub", "exp", "iss"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise TokenError("Token expirado") from exc
    except jwt.PyJWTError as exc:
        raise TokenError("Token inválido") from exc
