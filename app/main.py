"""API da Iniciadora de Pagamento (fluxo OAuth/redirect + jornadas JSR)."""

import base64
import json
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .config import settings
from .initiator import CoreBankingService
from .store import (
    ConsentRecord,
    DeviceRecord,
    get_active_device,
    get_by_consent_id,
    get_by_payment_id,
    get_by_request_id,
    get_device_by_enrollment_id,
    init_db,
    upsert_consent,
    upsert_device,
)

_service = CoreBankingService()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    yield


app = FastAPI(
    title="Payment Initiator (Open Finance)", version="0.2.0", lifespan=lifespan
)


class PaymentInitiationRequest(BaseModel):
    amount: str = Field(..., description="Valor em BRL, ex: '100.00'")
    currency: str = "BRL"
    creditor_name: str
    creditor_cpf_cnpj: str
    creditor_key: dict = Field(..., description="{type, value} da chave PIX do credor")
    account_id: str | None = Field(None, description="Override da conta de débito")


@app.post("/payments", status_code=201)
def create_payment(req: PaymentInitiationRequest) -> dict:
    """Inicia um pagamento: cria o auth request no core e devolve o login_url."""
    auth = _service.create_auth_request(settings.callback_url)

    payload = {
        "amount": req.amount,
        "currency": req.currency,
        "creditor_name": req.creditor_name,
        "creditor_cpf_cnpj": req.creditor_cpf_cnpj,
        "creditor_key": req.creditor_key,
        "account_id": req.account_id,
    }

    record = ConsentRecord(
        consent_id=auth["request_id"],  # provisório; atualizado no callback
        request_id=auth["request_id"],
        status="AWAITING_AUTH",
        payload=json.dumps(payload),
    )
    upsert_consent(record)

    return {
        "request_id": auth["request_id"],
        "login_url": auth["login_url"],
        "message": "Abra login_url no navegador para autenticar na detentora.",
    }


class JsrPaymentRequest(BaseModel):
    amount: str = Field(..., description="Valor em BRL, ex: '100.00'")
    currency: str = "BRL"
    creditor_name: str
    creditor_cpf_cnpj: str
    creditor_key: dict = Field(..., description="{type, value} da chave PIX do credor")


@app.post("/payments/jsr", status_code=201, response_model=None)
def create_jsr_payment(req: JsrPaymentRequest):
    """Inicia um pagamento JSR (sem redirect) usando o dispositivo vinculado.

    Exige um DeviceRecord REGISTERED. Caso contrário, devolve ``need_enrollment``
    com o ``login_url`` para o cliente cadastrar o dispositivo primeiro.
    """
    device = get_active_device()
    if not device:
        auth = _service.create_auth_request(settings.callback_url)
        return JSONResponse(
            status_code=400,
            content={
                "need_enrollment": True,
                "login_url": auth["login_url"],
                "message": "Nenhum dispositivo vinculado. Cadastre-o antes.",
            },
        )

    payload = {
        "amount": req.amount,
        "currency": req.currency,
        "creditor_name": req.creditor_name,
        "creditor_cpf_cnpj": req.creditor_cpf_cnpj,
        "creditor_key": req.creditor_key,
        "account_id": device.account_id,
        "kind": "jsr_payment",
    }

    # 1. Cria o consentimento JSR (contrato do core: jsConsentSchema)
    consent_data = _service.create_js_consent(
        {
            "accountId": device.account_id,
            "amount": req.amount,
            "description": f"PIX to {req.creditor_name}",
            "creditor": {
                "cpfCnpj": req.creditor_cpf_cnpj,
                "name": req.creditor_name,
            },
            "payment": {
                "amount": req.amount,
                "details": {
                    "localInstrument": "DICT",
                    "proxy": req.creditor_key["value"],
                    "creditorAccount": {
                        "number": req.creditor_key["value"],
                        "accountType": "CACC",
                    },
                },
            },
            "debtorAccount": {"number": device.account_id},
            "platform": "BROWSER",
        }
    )
    consent_id = consent_data.get("consent_id", "")

    # 2. Autoriza o consentimento com a credencial FIDO do dispositivo
    _service.authorise_js_consent(
        consent_id, device.credential_id, consent_data.get("fido_challenge")
    )

    # 3. Inicia o pagamento JSR
    payment = _service.initiate_js_payment(consent_id)

    record = ConsentRecord(
        consent_id=consent_id,
        request_id=str(uuid.uuid4()),
        status="COMPLETED",
        payment_id=payment.get("payment_id"),
        payload=json.dumps(payload),
    )
    upsert_consent(record)

    return {
        "payment_id": payment.get("payment_id"),
        "consent_id": consent_id,
        "status": "COMPLETED",
    }


class EnrollmentRequest(BaseModel):
    username: str = Field(..., description="Nome do titular a vincular (JSR/ITP)")
    account_number: str = Field("", description="Conta de débito do titular (opcional)")


@app.post("/enrollments", status_code=201)
def create_enrollment(req: EnrollmentRequest) -> dict:
    """Inicia o cadastro de um dispositivo (JSR/ITP) na detentora."""
    enrollment = _service.create_js_enrollment(settings.callback_url)
    auth = _service.create_auth_request(settings.callback_url)

    enrollment_id = enrollment.get("enrollment_id", "")
    record = ConsentRecord(
        consent_id=enrollment_id,
        request_id=auth["request_id"],
        status="ENROLLMENT_PENDING",
        payload=json.dumps(
            {
                "kind": "enrollment",
                "username": req.username,
                "account_number": req.account_number,
            }
        ),
    )
    upsert_consent(record)

    return {
        "enrollment_id": enrollment_id,
        "request_id": auth["request_id"],
        "login_url": auth["login_url"],
        "message": "Cadastre o dispositivo abrindo login_url na detentora.",
    }


def _decode_jwt_claims(token: str) -> dict:
    """Decodifica claims de um JWT (sem validar assinatura — demo/local)."""
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return {}
        padding = "=" * (-len(parts[1]) % 4)
        return json.loads(base64.urlsafe_b64decode(parts[1] + padding))
    except Exception:
        return {}


@app.get("/callback")
def callback(
    code: str = Query(...),
    state: str = Query(...),
) -> dict:
    """Recebe o redirect de volta do core e completa o fluxo em curso.

    - ``kind=enrollment``: conclui o cadastro do dispositivo (JSR/ITP).
    - senão: fluxo de pagamento com redirect (comportamento original).
    """
    record = get_by_request_id(state) or get_by_consent_id(state)
    if not record:
        raise HTTPException(
            status_code=404, detail="Requisição de pagamento não encontrada"
        )

    try:
        payload = json.loads(record.payload or "{}")
    except json.JSONDecodeError:
        payload = {}

    # 1. Trocar code por JWT
    jwt = _service.exchange_code(code)

    # -- Fluxo JSR/ITP: conclusão do cadastro do dispositivo ----------------
    if payload.get("kind") == "enrollment":
        return _complete_enrollment(record, payload, jwt)

    # -- Fluxo de pagamento com redirect (original) --------------------------

    # 2. Resolver a conta de débito (override ou primeira conta do usuário)
    account_id = payload.get("account_id")
    if not account_id:
        accounts = _service.list_accounts(jwt)
        if not accounts:
            raise HTTPException(status_code=400, detail="Usuário não possui contas")
        account_id = accounts[0]["id"]

    # 3. Criar o consentimento ASPSP
    consent_id = _service.create_aspsp_consent(
        jwt,
        {
            "account_id": account_id,
            "amount": payload["amount"],
            "creditor_name": payload["creditor_name"],
            "creditor_cpf_cnpj": payload.get("creditor_cpf_cnpj"),
            "creditor_key": payload["creditor_key"],
            "description": "Pagamento via Open Finance",
        },
    )

    # 4. Submeter o pagamento
    payment = _service.submit_aspsp_payment(jwt, consent_id)

    record.consent_id = consent_id
    record.status = "COMPLETED"
    record.code = code
    record.payment_id = payment.get("payment_id")
    upsert_consent(record)

    return {
        "payment_id": payment.get("payment_id"),
        "consent_id": consent_id,
        "status": "COMPLETED",
    }


def _complete_enrollment(record: ConsentRecord, payload: dict, jwt: str) -> dict:
    """Finaliza o cadastro do dispositivo (JSR/ITP) e persiste o DeviceRecord."""
    enrollment_id = record.consent_id
    claims = _decode_jwt_claims(jwt)
    # O titular deve ser o USUÁRIO AUTENTICADO (identidade do JWT), não um valor
    # arbitrário digitado no POST /enrollments nem o fallback "Cooperado".
    username = (
        claims.get("username")
        or claims.get("name")
        or payload.get("username")
        or "Cooperado"
    )
    account_number = payload.get("account_number", "")

    # Quando o número da conta não for informado no payload, resolve a conta de
    # débito real do usuário autenticado para vincular o enrollment ao titular.
    if not account_number:
        accounts = _service.list_accounts(jwt)
        if accounts:
            account_number = accounts[0].get("accountNumber", "") or ""

    # 1. Confirma o titular e obtém code+state do enrollment
    confirmed = _service.account_holder_confirmed_js(
        enrollment_id, username, account_number
    )
    authorization_code = confirmed.get("authorization_code", "")
    request_id = confirmed.get("request_id", record.request_id)

    # 2. Confirma o enrollment e recupera as opções FIDO
    reg_opts = _service.confirm_js_enrollment(
        enrollment_id, authorization_code, request_id
    )
    challenge = (
        reg_opts.get("fidoRegistrationOptions", {}).get("challenge", "")
        if isinstance(reg_opts, dict)
        else ""
    )

    # 3. Gera uma credencial FIDO (mock nesta demonstração)
    credential_id = "cred-" + uuid.uuid4().hex[:16]

    # 4. Registra o FIDO na detentora
    fido_response = {
        "id": credential_id,
        "rawId": credential_id,
        "type": "public-key",
        "attestationObject": "",
        "clientDataJSON": "",
        "challenge": challenge,
    }
    _service.register_js_fido(enrollment_id, fido_response)

    # 5. Resolve a conta de débito do titular
    accounts = _service.list_accounts(jwt)
    account_id = accounts[0]["id"] if accounts else ""

    # 6. Persiste o dispositivo como REGISTERED
    device = DeviceRecord(
        enrollment_id=enrollment_id,
        credential_id=credential_id,
        username=username,
        account_id=account_id,
        status="REGISTERED",
    )
    upsert_device(device)

    record.status = "DEVICE_REGISTERED"
    record.code = authorization_code
    upsert_consent(record)

    return {
        "enrollment_id": enrollment_id,
        "credential_id": credential_id,
        "account_id": account_id,
        "status": "DEVICE_REGISTERED",
    }


@app.get("/payments/{identifier}")
def get_payment(identifier: str) -> dict:
    """Consulta o status de um pagamento por payment_id ou consent_id."""
    record = get_by_payment_id(identifier) or get_by_consent_id(identifier)
    if not record:
        raise HTTPException(status_code=404, detail="Pagamento não encontrado")

    return {
        "consent_id": record.consent_id,
        "request_id": record.request_id,
        "status": record.status,
        "payment_id": record.payment_id,
        "created_at": record.created_at.isoformat(),
    }
