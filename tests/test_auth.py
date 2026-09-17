"""Autenticação por usuário/senha e access token JWT.

A Iniciadora passou a atender vários titulares, e cada chamada precisa dizer
quem está pagando. O que este arquivo trava: a senha não volta do hash, o token
só vale assinado pelo segredo certo e dentro da validade, e um token qualquer
não vira usuário autenticado.
"""

import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials

from app import auth, security
from app.config import settings
from app.store import UserRecord


# -- Senha ------------------------------------------------------------------


def test_the_hash_does_not_contain_the_password():
    encoded = security.hash_password("senha-secreta", iterations=1000)

    assert "senha-secreta" not in encoded
    assert encoded.startswith("pbkdf2_sha256$1000$")


def test_verifies_the_right_password_and_rejects_the_wrong_one():
    encoded = security.hash_password("senha-secreta", iterations=1000)

    assert security.verify_password("senha-secreta", encoded)
    assert not security.verify_password("outra-senha", encoded)


def test_the_same_password_hashes_differently_each_time():
    """Salt por usuário: dois titulares com a mesma senha não se denunciam."""
    primeiro = security.hash_password("igual", iterations=1000)
    segundo = security.hash_password("igual", iterations=1000)

    assert primeiro != segundo


def test_a_corrupt_hash_is_a_rejection_not_a_crash():
    assert not security.verify_password("x", "")
    assert not security.verify_password("x", "md5$1$sal$hash")
    assert not security.verify_password("x", "pbkdf2_sha256$abc")


# -- Token ------------------------------------------------------------------


def test_the_token_carries_the_user_and_comes_back_on_decode():
    token = security.create_access_token("alice")["access_token"]

    assert security.decode_access_token(token)["sub"] == "alice"


def test_rejects_a_token_signed_with_another_secret(monkeypatch):
    monkeypatch.setattr(settings, "jwt_secret", "segredo-do-atacante")
    forjado = security.create_access_token("alice")["access_token"]
    monkeypatch.setattr(settings, "jwt_secret", "segredo-de-verdade")

    with pytest.raises(security.TokenError):
        security.decode_access_token(forjado)


def test_rejects_an_expired_token():
    expirado = security.create_access_token("alice", expires_minutes=-1)[
        "access_token"
    ]

    with pytest.raises(security.TokenError):
        security.decode_access_token(expirado)


def test_rejects_garbage():
    with pytest.raises(security.TokenError):
        security.decode_access_token("nao-e-um-jwt")


# -- Usuários pré-cadastrados ------------------------------------------------


def test_reads_the_short_form_of_initiator_users():
    assert auth.parse_seed_users("alice:senha1, bruno:senha2") == [
        {"username": "alice", "password": "senha1", "full_name": ""},
        {"username": "bruno", "password": "senha2", "full_name": ""},
    ]


def test_reads_the_json_form_with_the_full_name():
    raw = '[{"username": "alice", "password": "s3nh4", "full_name": "Alice Silva"}]'

    assert auth.parse_seed_users(raw) == [
        {"username": "alice", "password": "s3nh4", "full_name": "Alice Silva"}
    ]


def test_ignores_entries_without_a_password():
    assert auth.parse_seed_users("alice,:senha,bruno:") == []


# -- authenticate_user / current_user ---------------------------------------


def _stored_user(monkeypatch, **kwargs) -> UserRecord:
    user = UserRecord(
        username="alice",
        password_hash=security.hash_password("senha-certa", iterations=1000),
        **kwargs,
    )
    monkeypatch.setattr(
        auth, "get_user", lambda username: user if username == user.username else None
    )
    return user


def test_authenticates_with_the_right_password(monkeypatch):
    user = _stored_user(monkeypatch)

    assert auth.authenticate_user("alice", "senha-certa") is user
    assert auth.authenticate_user("alice", "senha-errada") is None
    assert auth.authenticate_user("ninguem", "senha-certa") is None


def test_a_disabled_user_does_not_authenticate(monkeypatch):
    _stored_user(monkeypatch, disabled=True)

    assert auth.authenticate_user("alice", "senha-certa") is None


def _credentials(token: str) -> HTTPAuthorizationCredentials:
    return HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)


def test_current_user_resolves_the_token_owner(monkeypatch):
    user = _stored_user(monkeypatch)
    token = security.create_access_token("alice")["access_token"]

    assert auth.current_user(_credentials(token)) is user


def test_current_user_demands_a_token(monkeypatch):
    _stored_user(monkeypatch)

    with pytest.raises(HTTPException) as excinfo:
        auth.current_user(None)

    assert excinfo.value.status_code == 401


def test_current_user_rejects_a_token_of_someone_who_no_longer_exists(monkeypatch):
    """O usuário é relido a cada chamada: desativar alguém vale na hora."""
    _stored_user(monkeypatch)
    token = security.create_access_token("fantasma")["access_token"]

    with pytest.raises(HTTPException) as excinfo:
        auth.current_user(_credentials(token))

    assert excinfo.value.status_code == 401
