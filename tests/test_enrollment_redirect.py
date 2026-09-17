"""Volta da jornada de enrollment via ``redirect_uri`` do lojista.

O core sempre redireciona para o /callback DESTA Iniciadora. Quando o lojista
informa um ``redirect_uri`` (na allow-list) no POST /enrollments, o /callback
conclui o cadastro e devolve o navegador ao lojista — em vez de exibir JSON.
"""

import json

import pytest

import app.main as main
from app.store import ConsentRecord, UserRecord

USER = UserRecord(username="alice", password_hash="x", full_name="Alice")
LOJA = "https://sebo-frontend.vercel.app"


# -- allow-list -------------------------------------------------------------

def test_allowlist_vazia_nega_tudo(monkeypatch):
    monkeypatch.setattr(main.settings, "enrollment_redirect_allowlist", "")
    assert main._redirect_uri_allowed(f"{LOJA}/conta") is False


def test_allow_por_origem(monkeypatch):
    monkeypatch.setattr(
        main.settings, "enrollment_redirect_allowlist", f"{LOJA},http://localhost:5173"
    )
    # Mesma origem, qualquer caminho/query -> permitido.
    assert main._redirect_uri_allowed(f"{LOJA}/conta?enroll=return") is True
    assert main._redirect_uri_allowed("http://localhost:5173/x") is True
    # Origem diferente -> negado.
    assert main._redirect_uri_allowed("https://evil.com/x") is False
    # Truque de prefixo não engana (compara origem, não string).
    assert main._redirect_uri_allowed(f"https://evil.com/{LOJA}") is False
    # Sem esquema/host -> negado.
    assert main._redirect_uri_allowed("/conta") is False


# -- _client_redirect -------------------------------------------------------

def test_client_redirect_anexa_params_e_usa_303():
    resp = main._client_redirect(f"{LOJA}/conta", {"status": "DEVICE_REGISTERED", "enrollment_id": "e1"})
    assert resp.status_code == 303
    loc = resp.headers["location"]
    assert loc.startswith(f"{LOJA}/conta?")
    assert "status=DEVICE_REGISTERED" in loc and "enrollment_id=e1" in loc


def test_client_redirect_preserva_query_existente():
    resp = main._client_redirect(f"{LOJA}/conta?enroll=return", {"status": "error"})
    assert "conta?enroll=return&status=error" in resp.headers["location"]


# -- POST /enrollments: validação do redirect_uri ---------------------------

def _stub_enrollment_services(monkeypatch, captured):
    monkeypatch.setattr(
        main._service, "create_js_enrollment", lambda _u: {"enrollment_id": "enr-1"}
    )
    monkeypatch.setattr(
        main._service, "create_auth_request", lambda _u: {"request_id": "req-1", "login_url": "http://login"}
    )
    monkeypatch.setattr(main, "upsert_consent", lambda record: captured.setdefault("record", record))


def test_enrollment_rejeita_redirect_fora_da_allowlist(monkeypatch):
    monkeypatch.setattr(main.settings, "enrollment_redirect_allowlist", LOJA)
    req = main.EnrollmentRequest(redirect_uri="https://evil.com/x")
    with pytest.raises(main.HTTPException) as exc:
        main.create_enrollment(req, USER)
    assert exc.value.status_code == 400


def test_enrollment_guarda_redirect_permitido(monkeypatch):
    monkeypatch.setattr(main.settings, "enrollment_redirect_allowlist", LOJA)
    captured = {}
    _stub_enrollment_services(monkeypatch, captured)

    req = main.EnrollmentRequest(redirect_uri=f"{LOJA}/conta?enroll=return")
    out = main.create_enrollment(req, USER)

    assert out["enrollment_id"] == "enr-1"
    payload = json.loads(captured["record"].payload)
    assert payload["client_redirect_uri"] == f"{LOJA}/conta?enroll=return"


# -- GET /callback: redireciona ao lojista ao concluir ----------------------

def _enrollment_record(client_redirect_uri: str) -> ConsentRecord:
    return ConsentRecord(
        owner=USER.username,
        consent_id="enr-1",
        request_id="req-1",
        status="ENROLLMENT_PENDING",
        payload=json.dumps(
            {"kind": "enrollment", "username": "Alice", "client_redirect_uri": client_redirect_uri}
        ),
    )


def test_callback_redireciona_ao_lojista(monkeypatch):
    record = _enrollment_record(f"{LOJA}/conta?enroll=return")
    monkeypatch.setattr(main, "get_by_request_id", lambda _s: record)
    monkeypatch.setattr(main._service, "exchange_code", lambda _c: "jwt")
    monkeypatch.setattr(
        main, "_complete_enrollment",
        lambda *a, **k: {"status": "DEVICE_REGISTERED", "enrollment_id": "enr-1"},
    )

    resp = main.callback(code="code-1", state="req-1")

    assert resp.status_code == 303
    loc = resp.headers["location"]
    assert loc.startswith(f"{LOJA}/conta?enroll=return")
    assert "status=DEVICE_REGISTERED" in loc and "enrollment_id=enr-1" in loc


def test_callback_sem_redirect_mantem_json(monkeypatch):
    record = _enrollment_record("")  # sem redirect_uri
    monkeypatch.setattr(main, "get_by_request_id", lambda _s: record)
    monkeypatch.setattr(main._service, "exchange_code", lambda _c: "jwt")
    marker = {"status": "DEVICE_REGISTERED", "enrollment_id": "enr-1"}
    monkeypatch.setattr(main, "_complete_enrollment", lambda *a, **k: marker)

    resp = main.callback(code="code-1", state="req-1")

    assert resp == marker  # dict cru, sem redirect
