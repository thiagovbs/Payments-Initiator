"""Jornada de pagamento com redirecionamento: consentimento antes do dinheiro.

O que estes testes prendem é a ordem. Antes, o titular só fazia login na
detentora e o pagamento era criado e submetido no /callback, depois dele — a
pessoa autenticava sem nunca ver valor nem credor. Agora o consentimento é
criado primeiro, fica pendente, e o /callback só submete quando a detentora
devolve ``status=AUTHORISED``.
"""

import json

import pytest

import app.main as main
from app.store import ConsentRecord, UserRecord

USER = UserRecord(username="alice", password_hash="x", full_name="Alice")
LOJA = "https://sebo-frontend.vercel.app"
CONSENT_ID = "55555555-5555-4555-8555-555555555555"

PAYMENT = main.PaymentInitiationRequest(
    amount="25.00",
    creditor_name="Loja Exemplo",
    creditor_cpf_cnpj="11222333000181",
    creditor_key={"type": "EMAIL", "value": "loja@example.com"},
)


# -- POST /payments: cria o consentimento, não paga -------------------------


def test_payments_cria_consentimento_pendente_e_devolve_url_de_aprovacao(monkeypatch):
    captured = {}

    def fake_create(payload):
        captured["payload"] = payload
        return {
            "consent_id": CONSENT_ID,
            "status": "AWAITING_AUTHORISATION",
            "authorisation_url": f"http://core.local/v1/aspsp/payments/consents/{CONSENT_ID}/authorise",
        }

    monkeypatch.setattr(main._service, "create_aspsp_consent", fake_create)
    monkeypatch.setattr(main, "upsert_consent", lambda record: captured.setdefault("record", record))
    # Se a Iniciadora tentasse pagar aqui, o teste quebra.
    monkeypatch.setattr(
        main._service,
        "submit_aspsp_payment",
        lambda *a, **k: pytest.fail("não pode submeter antes da aprovação"),
    )

    result = main.create_payment(PAYMENT, USER)

    assert result["consent_id"] == CONSENT_ID
    assert result["status"] == "AWAITING_AUTHORISATION"
    assert "authorise" in result["authorisation_url"]
    # A conta de débito não é escolhida aqui: quem escolhe é o titular na tela.
    assert "account_id" not in captured["payload"]
    assert captured["record"].status == "AWAITING_AUTHORISATION"


def test_payments_recusa_redirect_uri_fora_da_allowlist(monkeypatch):
    monkeypatch.setattr(main.settings, "enrollment_redirect_allowlist", LOJA)
    request = PAYMENT.model_copy(update={"redirect_uri": "https://evil.com/volta"})
    monkeypatch.setattr(
        main._service,
        "create_aspsp_consent",
        lambda *a, **k: pytest.fail("não pode criar consentimento com redirect inválido"),
    )

    with pytest.raises(main.HTTPException) as exc:
        main.create_payment(request, USER)

    assert exc.value.status_code == 400


# -- GET /callback: submete só o que foi aprovado ---------------------------


def _payment_record(client_redirect_uri: str = "") -> ConsentRecord:
    return ConsentRecord(
        owner=USER.username,
        consent_id=CONSENT_ID,
        request_id="req-1",
        status="AWAITING_AUTHORISATION",
        payload=json.dumps(
            {"kind": "redirect_payment", "client_redirect_uri": client_redirect_uri}
        ),
    )


def test_callback_submete_o_pagamento_quando_aprovado(monkeypatch):
    record = _payment_record()
    monkeypatch.setattr(main, "get_by_consent_id", lambda _c: record)
    monkeypatch.setattr(main, "upsert_consent", lambda r: r)
    monkeypatch.setattr(
        main._service,
        "submit_aspsp_payment",
        lambda consent_id: {"payment_id": "pay-1", "consent_id": consent_id},
    )

    result = main.callback(consentId=CONSENT_ID, status="AUTHORISED")

    assert result["status"] == "COMPLETED"
    assert result["payment_id"] == "pay-1"
    assert record.status == "COMPLETED"


def test_callback_nao_paga_quando_o_titular_recusa(monkeypatch):
    record = _payment_record()
    monkeypatch.setattr(main, "get_by_consent_id", lambda _c: record)
    monkeypatch.setattr(main, "upsert_consent", lambda r: r)
    monkeypatch.setattr(
        main._service,
        "submit_aspsp_payment",
        lambda *a, **k: pytest.fail("não pode pagar um consentimento recusado"),
    )

    result = main.callback(consentId=CONSENT_ID, status="REJECTED")

    assert result["status"] == "REJECTED"
    assert record.status == "REJECTED"


def test_callback_devolve_o_navegador_ao_lojista(monkeypatch):
    record = _payment_record(f"{LOJA}/checkout?voltando=1")
    monkeypatch.setattr(main, "get_by_consent_id", lambda _c: record)
    monkeypatch.setattr(main, "upsert_consent", lambda r: r)
    monkeypatch.setattr(
        main._service,
        "submit_aspsp_payment",
        lambda consent_id: {"payment_id": "pay-1", "consent_id": consent_id},
    )

    resp = main.callback(consentId=CONSENT_ID, status="AUTHORISED")

    assert resp.status_code == 303
    location = resp.headers["location"]
    assert location.startswith(f"{LOJA}/checkout?voltando=1")
    assert "status=COMPLETED" in location and "payment_id=pay-1" in location


def test_callback_sem_identificacao_falha(monkeypatch):
    with pytest.raises(main.HTTPException) as exc:
        main.callback()

    assert exc.value.status_code == 400


def test_callback_de_pagamento_nao_aceita_mais_code_state(monkeypatch):
    """O formato antigo criava o consentimento já autorizado; não existe mais."""
    record = _payment_record()
    monkeypatch.setattr(main, "get_by_request_id", lambda _s: record)
    monkeypatch.setattr(main, "get_by_consent_id", lambda _c: record)
    monkeypatch.setattr(
        main._service,
        "exchange_code",
        lambda *a, **k: pytest.fail("não deve trocar code em pagamento"),
    )

    with pytest.raises(main.HTTPException) as exc:
        main.callback(code="code-1", state="req-1")

    assert exc.value.status_code == 400
