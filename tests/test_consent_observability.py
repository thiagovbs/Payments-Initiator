"""Observabilidade do consentimento: webhook da detentora e reconciliação.

Duas lacunas que estes testes fecham. A primeira: o registro local só sabia o
que passou por esta aplicação, então titular que aprovava e fechava o navegador
deixava o lojista vendo "pendente" para sempre. A segunda: o aviso da detentora
é acionável (a Iniciadora submete o pagamento em cima dele), então ele precisa
ser autenticado — senão qualquer um postaria ``AUTHORISED``.
"""

import asyncio
import hashlib
import hmac
import json

import httpx
import pytest

import app.main as main
from app.store import ConsentRecord, UserRecord

USER = UserRecord(username="alice", password_hash="x", full_name="Alice")
CONSENT_ID = "55555555-5555-4555-8555-555555555555"
SECRET = "test-initiator-secret"


class FakeRequest:
    """O mínimo de ``fastapi.Request`` que o webhook usa: corpo cru e headers."""

    def __init__(self, body: bytes, headers: dict):
        self._body = body
        self.headers = headers

    async def body(self) -> bytes:
        return self._body


def _record(status: str = "AWAITING_AUTHORISATION") -> ConsentRecord:
    return ConsentRecord(
        owner=USER.username,
        consent_id=CONSENT_ID,
        request_id="req-1",
        status=status,
        payload=json.dumps({"kind": "redirect_payment", "client_redirect_uri": ""}),
    )


def _signed(event: dict, secret: str = SECRET) -> FakeRequest:
    body = json.dumps(event).encode("utf-8")
    signature = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return FakeRequest(body, {"x-webhook-signature": signature})


@pytest.fixture(autouse=True)
def _secret(monkeypatch):
    monkeypatch.setattr(main.settings, "initiator_client_secret", SECRET)


# -- assinatura -------------------------------------------------------------


def test_assinatura_valida_e_aceita():
    body = b'{"status":"AUTHORISED"}'
    signature = hmac.new(SECRET.encode("utf-8"), body, hashlib.sha256).hexdigest()
    assert main.verify_webhook_signature(body, signature) is True


def test_assinatura_de_outro_corpo_e_recusada():
    outro = hmac.new(SECRET.encode("utf-8"), b'{"status":"REJECTED"}', hashlib.sha256)
    assert main.verify_webhook_signature(b'{"status":"AUTHORISED"}', outro.hexdigest()) is False


def test_sem_assinatura_e_recusado():
    assert main.verify_webhook_signature(b"{}", None) is False


# -- webhook ----------------------------------------------------------------


def test_webhook_sem_assinatura_nao_paga(monkeypatch):
    monkeypatch.setattr(main, "get_by_consent_id", lambda _c: _record())
    monkeypatch.setattr(
        main._service,
        "submit_aspsp_payment",
        lambda *a, **k: pytest.fail("aviso não autenticado não pode pagar"),
    )
    request = FakeRequest(json.dumps({"consentId": CONSENT_ID, "status": "AUTHORISED"}).encode(), {})

    with pytest.raises(main.HTTPException) as exc:
        asyncio.run(main.consent_webhook(request))

    assert exc.value.status_code == 401


def test_webhook_autorizado_submete_o_pagamento(monkeypatch):
    record = _record()
    monkeypatch.setattr(main, "get_by_consent_id", lambda _c: record)
    monkeypatch.setattr(main, "upsert_consent", lambda r: r)
    monkeypatch.setattr(
        main._service,
        "submit_aspsp_payment",
        lambda consent_id: {"payment_id": "pay-1", "consent_id": consent_id},
    )

    result = asyncio.run(
        main.consent_webhook(_signed({"consentId": CONSENT_ID, "status": "AUTHORISED"}))
    )

    # É isto que fecha a jornada quando o navegador não volta pelo /callback.
    assert result["status"] == "COMPLETED"
    assert result["payment_id"] == "pay-1"
    assert record.status == "COMPLETED"


def test_webhook_recusado_nao_paga(monkeypatch):
    record = _record()
    monkeypatch.setattr(main, "get_by_consent_id", lambda _c: record)
    monkeypatch.setattr(main, "upsert_consent", lambda r: r)
    monkeypatch.setattr(
        main._service,
        "submit_aspsp_payment",
        lambda *a, **k: pytest.fail("consentimento recusado não pode pagar"),
    )

    asyncio.run(main.consent_webhook(_signed({"consentId": CONSENT_ID, "status": "REJECTED"})))

    assert record.status == "REJECTED"


def test_webhook_nao_submete_de_novo_o_que_ja_foi_pago(monkeypatch):
    record = _record(status="COMPLETED")
    monkeypatch.setattr(main, "get_by_consent_id", lambda _c: record)
    monkeypatch.setattr(main, "upsert_consent", lambda r: r)
    monkeypatch.setattr(
        main._service,
        "submit_aspsp_payment",
        lambda *a, **k: pytest.fail("não pode submeter duas vezes"),
    )

    result = asyncio.run(
        main.consent_webhook(_signed({"consentId": CONSENT_ID, "status": "AUTHORISED"}))
    )

    assert result["status"] == "COMPLETED"


def test_webhook_tolera_corrida_com_o_callback(monkeypatch):
    """409 da detentora = o /callback chegou primeiro. Não é falha."""
    record = _record()
    monkeypatch.setattr(main, "get_by_consent_id", lambda _c: record)
    monkeypatch.setattr(main, "upsert_consent", lambda r: r)

    def conflict(_consent_id):
        request = httpx.Request("POST", "http://core.local/v1/aspsp/payments")
        response = httpx.Response(409, request=request, content=b'{"error":"CONSENT_NOT_AUTHORISED"}')
        raise httpx.HTTPStatusError("conflito", request=request, response=response)

    monkeypatch.setattr(main._service, "submit_aspsp_payment", conflict)

    result = asyncio.run(
        main.consent_webhook(_signed({"consentId": CONSENT_ID, "status": "AUTHORISED"}))
    )

    assert result["received"] is True
    assert record.status != "SUBMISSION_FAILED"


def test_webhook_de_consentimento_desconhecido_responde_200(monkeypatch):
    monkeypatch.setattr(main, "get_by_consent_id", lambda _c: None)

    result = asyncio.run(
        main.consent_webhook(_signed({"consentId": "outro", "status": "AUTHORISED"}))
    )

    # 4xx só faria a detentora reenviar um aviso que nunca será nosso.
    assert result == {"received": True, "known": False}


# -- reconciliação ----------------------------------------------------------


def test_consulta_traz_o_estado_da_detentora_e_persiste(monkeypatch):
    record = _record()
    monkeypatch.setattr(main, "get_by_payment_id", lambda *a, **k: record)
    monkeypatch.setattr(main, "get_by_consent_id", lambda *a, **k: record)
    monkeypatch.setattr(main, "upsert_consent", lambda r: r)
    monkeypatch.setattr(
        main._service,
        "get_aspsp_consent",
        lambda _c: {"consentId": CONSENT_ID, "status": "AUTHORISED", "paymentId": None},
    )

    result = main.get_payment(CONSENT_ID, USER)

    # A cópia local dizia AWAITING_AUTHORISATION; a verdade está no core.
    assert result["status"] == "AUTHORISED"
    assert result["source"] == "core"
    assert record.status == "AUTHORISED"


def test_consulta_degrada_para_a_copia_local_se_o_core_nao_responde(monkeypatch):
    record = _record(status="COMPLETED")
    record.payment_id = "pay-1"
    monkeypatch.setattr(main, "get_by_payment_id", lambda *a, **k: record)
    monkeypatch.setattr(main, "get_by_consent_id", lambda *a, **k: record)

    def unreachable(_c):
        raise httpx.ConnectError("sem rota")

    monkeypatch.setattr(main._service, "get_aspsp_consent", unreachable)

    result = main.get_payment(CONSENT_ID, USER)

    # Resposta velha e avisada disso é melhor que 502 na cara do lojista.
    assert result["status"] == "COMPLETED"
    assert result["source"] == "local"


def test_trilha_de_eventos_vem_da_detentora(monkeypatch):
    record = _record()
    monkeypatch.setattr(main, "get_by_payment_id", lambda *a, **k: record)
    monkeypatch.setattr(main, "get_by_consent_id", lambda *a, **k: record)
    trail = {"consentId": CONSENT_ID, "events": [{"event": "CONSENT_CREATED"}]}
    monkeypatch.setattr(main._service, "get_aspsp_consent_events", lambda _c: trail)

    assert main.get_payment_events(CONSENT_ID, USER) == trail
