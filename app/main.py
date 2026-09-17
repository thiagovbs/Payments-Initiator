"""API da Iniciadora de Pagamento (fluxo OAuth/redirect + jornadas JSR)."""

import base64
import json
import uuid
from contextlib import asynccontextmanager
from urllib.parse import urlencode, urlsplit

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, Field

from .auth import current_user, seed_users
from .auth import router as auth_router
from .config import settings
from .errors import register_error_handlers
from .initiator import CoreBankingService
from .store import (
    ConsentRecord,
    DeviceRecord,
    UserRecord,
    get_by_consent_id,
    get_by_payment_id,
    get_by_request_id,
    get_device_by_enrollment_id,
    init_db,
    list_devices,
    upsert_consent,
    upsert_device,
)

_service = CoreBankingService()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    # Os usuários pré-cadastrados vêm do ambiente e são regravados a cada
    # start, para que trocar uma senha em INITIATOR_USERS passe a valer.
    seed_users()
    yield


app = FastAPI(
    title="Payment Initiator (Open Finance)", version="0.3.0", lifespan=lifespan
)

# Login e consulta do próprio usuário. Todas as demais rotas exigem o JWT que
# esta rota emite — exceto /callback, que é aberta pelo navegador do titular
# vindo da detentora e não tem como carregar o header Authorization.
app.include_router(auth_router)

# Erros vindos do core-banking chegam ao cliente com o motivo original, em vez
# de virarem um 500 opaco.
register_error_handlers(app)


class PaymentInitiationRequest(BaseModel):
    amount: str = Field(..., description="Valor em BRL, ex: '100.00'")
    currency: str = "BRL"
    creditor_name: str
    creditor_cpf_cnpj: str
    creditor_key: dict = Field(..., description="{type, value} da chave PIX do credor")
    account_id: str | None = Field(None, description="Override da conta de débito")


@app.post("/payments", status_code=201)
def create_payment(
    req: PaymentInitiationRequest, user: UserRecord = Depends(current_user)
) -> dict:
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
        owner=user.username,
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
    enrollment_id: str = Field(
        ...,
        description="Dispositivo vinculado que autoriza o pagamento. "
        "Devolvido por POST /enrollments e pelo GET /callback do cadastro.",
    )


@app.post("/payments/jsr", status_code=201, response_model=None)
def create_jsr_payment(
    req: JsrPaymentRequest, user: UserRecord = Depends(current_user)
):
    """Inicia um pagamento JSR (sem redirect) com o dispositivo informado.

    O ``enrollment_id`` é obrigatório e identifica de quem é o pagamento. Antes,
    usava-se o dispositivo REGISTERED mais recente, sem escopo de titular: com
    mais de um titular cadastrado, o pagamento de um saía da conta do outro.

    A busca é restrita aos dispositivos do usuário autenticado: um
    ``enrollment_id`` de outro titular responde como inexistente, em vez de
    debitar a conta dele.
    """
    device = get_device_by_enrollment_id(req.enrollment_id, user.username)
    if not device or device.status != "REGISTERED":
        auth = _service.create_auth_request(settings.callback_url)
        return JSONResponse(
            status_code=404 if not device else 409,
            content={
                "need_enrollment": True,
                "login_url": auth["login_url"],
                "message": (
                    "Dispositivo não encontrado. Cadastre-o antes."
                    if not device
                    else f"Dispositivo está {device.status}, não REGISTERED."
                ),
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
            # O enrollment e a ancora: a Detentora tira conta e titular dele.
            # O accountId vai junto apenas para ser conferido -- divergencia e
            # recusada com 400, em vez de debitar outra conta em silencio.
            "enrollmentId": device.enrollment_id,
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
        consent_id, device.credential_id, consent_data.get("fido_challenge", "")
    )

    # 3. Inicia o pagamento JSR
    payment = _service.initiate_js_payment(consent_id)

    record = ConsentRecord(
        owner=user.username,
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
    username: str = Field(
        "",
        description="Nome do titular a vincular na detentora (JSR/ITP). "
        "Se vazio, usa o usuário autenticado nesta Iniciadora.",
    )
    account_number: str = Field("", description="Conta de débito do titular (opcional)")
    redirect_uri: str = Field(
        "",
        description="Para onde o /callback deve redirecionar o navegador do "
        "titular ao concluir o enrollment (permite ao lojista personalizar a "
        "volta da jornada). Precisa estar na allow-list da Iniciadora; vazio "
        "mantém a resposta JSON.",
    )


@app.post("/enrollments", status_code=201)
def create_enrollment(
    req: EnrollmentRequest, user: UserRecord = Depends(current_user)
) -> dict:
    """Inicia o cadastro de um dispositivo (JSR/ITP) na detentora."""
    # Destino de volta da jornada (opcional). O core sempre redireciona para o
    # /callback DESTA Iniciadora; o redirect_uri é o salto seguinte, de volta ao
    # lojista, e por isso precisa estar na allow-list (evita open redirect).
    if req.redirect_uri and not _redirect_uri_allowed(req.redirect_uri):
        raise HTTPException(
            status_code=400,
            detail="redirect_uri não permitido (fora da allow-list da Iniciadora)",
        )

    enrollment = _service.create_js_enrollment(settings.callback_url)
    auth = _service.create_auth_request(settings.callback_url)

    enrollment_id = enrollment.get("enrollment_id", "")
    record = ConsentRecord(
        owner=user.username,
        consent_id=enrollment_id,
        request_id=auth["request_id"],
        status="ENROLLMENT_PENDING",
        payload=json.dumps(
            {
                "kind": "enrollment",
                "username": req.username or user.full_name or user.username,
                "account_number": req.account_number,
                "client_redirect_uri": req.redirect_uri,
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


class DeviceSummary(BaseModel):
    enrollment_id: str
    credential_id: str
    username: str
    account_id: str
    status: str


@app.get("/enrollments", response_model=list[DeviceSummary])
def list_enrollments(user: UserRecord = Depends(current_user)) -> list[DeviceSummary]:
    """Lista os dispositivos vinculados do usuário autenticado.

    É por aqui que se recupera o ``enrollment_id`` exigido no pagamento JSR,
    sem precisar guardar a resposta do cadastro.
    """
    return [
        DeviceSummary(
            enrollment_id=device.enrollment_id,
            credential_id=device.credential_id,
            username=device.username,
            account_id=device.account_id,
            status=device.status,
        )
        for device in list_devices(user.username)
    ]


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


def _redirect_uri_allowed(uri: str) -> bool:
    """Diz se o ``uri`` está na allow-list de redirect do enrollment.

    Compara **origem** (esquema + host[:porta]), não prefixo de string, para não
    cair em truques do tipo ``https://evil.com/https://lojista``. Allow-list
    vazia = nada é permitido (o /callback fica só no JSON).
    """
    allow = settings.enrollment_redirect_allowlist
    if not allow:
        return False
    parts = urlsplit(uri)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return False
    origin = f"{parts.scheme}://{parts.netloc}"
    allowed = set()
    for entry in allow.split(","):
        entry = entry.strip()
        if not entry:
            continue
        p = urlsplit(entry)
        allowed.add(f"{p.scheme}://{p.netloc}" if p.scheme and p.netloc else entry)
    return origin in allowed


def _client_redirect(redirect_uri: str, params: dict) -> RedirectResponse:
    """Redireciona o navegador do titular de volta ao lojista, com o resultado.

    Usa 303 para virar um GET na volta, e preserva a query que o lojista já
    tenha posto no ``redirect_uri``.
    """
    query = urlencode({k: v for k, v in params.items() if v})
    if query:
        sep = "&" if urlsplit(redirect_uri).query else "?"
        redirect_uri = f"{redirect_uri}{sep}{query}"
    return RedirectResponse(redirect_uri, status_code=303)


@app.get("/callback", response_model=None)
def callback(
    code: str = Query(...),
    state: str = Query(...),
) -> object:
    """Recebe o redirect de volta do core e completa o fluxo em curso.

    - ``kind=enrollment``: conclui o cadastro do dispositivo (JSR/ITP).
    - senão: fluxo de pagamento com redirect (comportamento original).

    Esta é a única rota sem JWT: quem a chama é o navegador do titular, vindo
    da detentora, e um redirect não carrega o header ``Authorization``. O que
    a amarra a um usuário é o ``state`` — o ``request_id`` que só existe no
    registro criado por uma chamada já autenticada, e de onde sai o dono do
    dispositivo/pagamento resultante.
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
        client_redirect = payload.get("client_redirect_uri")
        if client_redirect:
            # Personaliza a volta da jornada: conclui o cadastro e devolve o
            # navegador ao lojista (com o resultado na query), em vez de exibir
            # JSON cru na Iniciadora.
            try:
                result = _complete_enrollment(record, payload, jwt)
            except HTTPException as exc:
                return _client_redirect(
                    client_redirect,
                    {"status": "error", "detail": str(exc.detail)},
                )
            return _client_redirect(
                client_redirect,
                {
                    "status": result.get("status", "DEVICE_REGISTERED"),
                    "enrollment_id": result.get("enrollment_id", ""),
                },
            )
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

    # 6. Persiste o dispositivo como REGISTERED, sob o usuário que pediu o
    # cadastro (o dono do ConsentRecord), que é quem poderá pagar com ele.
    device = DeviceRecord(
        owner=record.owner,
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
def get_payment(identifier: str, user: UserRecord = Depends(current_user)) -> dict:
    """Consulta o status de um pagamento por payment_id ou consent_id.

    Só enxerga os pagamentos do próprio usuário: o identificador de outro
    titular responde 404, sem revelar que ele existe.
    """
    record = get_by_payment_id(identifier, user.username) or get_by_consent_id(
        identifier, user.username
    )
    if not record:
        raise HTTPException(status_code=404, detail="Pagamento não encontrado")

    return {
        "consent_id": record.consent_id,
        "request_id": record.request_id,
        "status": record.status,
        "payment_id": record.payment_id,
        "created_at": record.created_at.isoformat(),
    }
