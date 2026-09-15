"""Seleção do dispositivo no pagamento JSR.

O pagamento usava ``get_active_device()``, que devolvia o dispositivo
REGISTERED mais recente sem nenhum escopo de titular: com dois titulares
cadastrados, o pagamento de um saía da conta do outro. O ``enrollment_id``
passou a ser obrigatório no corpo.

Isso importa mais depois que a Detentora passou a amarrar a conta ao
enrollment: ela debita corretamente a conta daquele enrollment, então escolher
o enrollment errado aqui debita a conta errada lá.
"""

import app.main as main
from app.store import DeviceRecord


def _device(enrollment_id: str, account_id: str, status: str = "REGISTERED"):
    return DeviceRecord(
        enrollment_id=enrollment_id,
        credential_id=f"cred-{enrollment_id}",
        username=f"user-{enrollment_id}",
        account_id=account_id,
        status=status,
    )


def _request(enrollment_id: str) -> main.JsrPaymentRequest:
    return main.JsrPaymentRequest(
        amount="25.00",
        creditor_name="Beneficiario",
        creditor_cpf_cnpj="01688166360",
        creditor_key={"type": "CPF", "value": "01688166360"},
        enrollment_id=enrollment_id,
    )


def test_enrollment_id_is_required():
    import pydantic
    import pytest

    with pytest.raises(pydantic.ValidationError):
        main.JsrPaymentRequest(
            amount="25.00",
            creditor_name="Beneficiario",
            creditor_cpf_cnpj="01688166360",
            creditor_key={"type": "CPF", "value": "01688166360"},
        )


def test_uses_the_device_named_in_the_body(monkeypatch):
    devices = {
        "enroll-a": _device("enroll-a", "acc-a"),
        "enroll-b": _device("enroll-b", "acc-b"),
    }
    monkeypatch.setattr(main, "get_device_by_enrollment_id", devices.get)

    sent = {}

    def fake_create_consent(payload):
        sent.update(payload)
        return {"consent_id": "consent-1", "fido_challenge": "challenge"}

    monkeypatch.setattr(main._service, "create_js_consent", fake_create_consent)
    monkeypatch.setattr(main._service, "authorise_js_consent", lambda *a, **k: None)
    monkeypatch.setattr(
        main._service, "initiate_js_payment", lambda _c: {"payment_id": "pay-1"}
    )
    monkeypatch.setattr(main, "upsert_consent", lambda record: record)

    # Pede explicitamente o dispositivo B, embora A tambem exista.
    main.create_jsr_payment(_request("enroll-b"))

    assert sent["enrollmentId"] == "enroll-b"
    assert sent["accountId"] == "acc-b"


def test_reports_404_for_an_unknown_device(monkeypatch):
    monkeypatch.setattr(main, "get_device_by_enrollment_id", lambda _id: None)
    monkeypatch.setattr(
        main._service, "create_auth_request", lambda _url: {"login_url": "http://login"}
    )

    response = main.create_jsr_payment(_request("enroll-desconhecido"))

    assert response.status_code == 404


def test_reports_409_for_a_device_that_is_not_registered(monkeypatch):
    pending = _device("enroll-a", "acc-a", status="PENDING")
    monkeypatch.setattr(main, "get_device_by_enrollment_id", lambda _id: pending)
    monkeypatch.setattr(
        main._service, "create_auth_request", lambda _url: {"login_url": "http://login"}
    )

    response = main.create_jsr_payment(_request("enroll-a"))

    assert response.status_code == 409
