"""API da Iniciadora de Pagamento (fluxo OAuth/redirect do core)."""

import json
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

from .config import settings
from .initiator import CoreBankingService
from .store import (
    ConsentRecord,
    get_by_consent_id,
    get_by_payment_id,
    get_by_request_id,
    init_db,
    upsert_consent,
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


@app.get("/callback")
def callback(
    code: str = Query(...),
    state: str = Query(...),
) -> dict:
    """Recebe o redirect de volta do core e completa o fluxo de pagamento."""
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
