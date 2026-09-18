"""API da Iniciadora de Pagamento (fluxo OAuth/redirect + jornadas JSR)."""

import asyncio
import base64
import hashlib
import hmac
import json
import uuid
from contextlib import asynccontextmanager
from typing import Annotated
from urllib.parse import urlencode, urlsplit

import httpx
from fastapi import Depends, FastAPI, HTTPException, Query, Request
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
    delete_device_by_enrollment_id,
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
    debtor_cpf: str = Field(
        "",
        description="CPF do pagador, quando conhecido. Informado, só esse "
        "titular consegue aprovar o consentimento na detentora.",
    )
    redirect_uri: str = Field(
        "",
        description="Para onde o /callback deve redirecionar o navegador do "
        "titular depois da aprovação (permite ao lojista personalizar a volta "
        "da jornada). Precisa estar na allow-list; vazio mantém a resposta JSON.",
    )


@app.post("/payments", status_code=201)
def create_payment(
    req: PaymentInitiationRequest, user: UserRecord = Depends(current_user)
) -> dict:
    """Inicia um pagamento com redirecionamento: cria o **consentimento** e
    devolve a URL onde o titular o aprova.

    A ordem importa e mudou. Antes, o titular só fazia login na detentora e o
    consentimento era criado já autorizado no /callback, depois dele: a pessoa
    autenticava sem nunca ver valor nem credor, e o dinheiro saía. Agora o
    consentimento nasce ``AWAITING_AUTHORISATION`` **antes** do redirect, e a
    ``authorisation_url`` é uma tela que mostra o que está sendo pago e pede
    confirmação. Sem essa confirmação a detentora recusa a submissão.

    A conta de débito não é mais escolhida aqui: quem escolhe é o titular, na
    tela, entre as contas dele.
    """
    if req.redirect_uri and not _redirect_uri_allowed(req.redirect_uri):
        raise HTTPException(
            status_code=400,
            detail="redirect_uri não permitido (fora da allow-list da Iniciadora)",
        )

    payload = {
        "amount": req.amount,
        "currency": req.currency,
        "creditor_name": req.creditor_name,
        "creditor_cpf_cnpj": req.creditor_cpf_cnpj,
        "creditor_key": req.creditor_key,
        "kind": "redirect_payment",
        "client_redirect_uri": req.redirect_uri,
    }

    consent = _service.create_aspsp_consent(
        {
            **payload,
            "description": f"PIX to {req.creditor_name}",
            "debtor_cpf": req.debtor_cpf,
            # A detentora devolve o navegador para cá com o desfecho, e é aqui
            # que o pagamento é submetido.
            "redirect_uri": settings.callback_url,
            # ...e avisa por aqui mesmo que o navegador não volte.
            "webhook_uri": settings.webhook_url,
        }
    )

    record = ConsentRecord(
        owner=user.username,
        consent_id=consent["consent_id"],
        request_id=str(uuid.uuid4()),
        status=consent.get("status", "AWAITING_AUTHORISATION"),
        payload=json.dumps(payload),
    )
    upsert_consent(record)

    return {
        "consent_id": consent["consent_id"],
        "status": record.status,
        "authorisation_url": consent["authorisation_url"],
        "message": (
            "Abra authorisation_url no navegador: o titular revisa o valor e o "
            "credor e aprova. O pagamento só é submetido depois disso."
        ),
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


@app.delete("/enrollments/{enrollment_id}")
def revoke_enrollment(
    enrollment_id: str, user: UserRecord = Depends(current_user)
) -> dict:
    """Revoga um dispositivo (enrollment JSR) do usuário autenticado.

    Remove o vínculo guardado nesta Iniciadora — o que impede novos pagamentos
    JSR com ele — e pede, best-effort, a revogação na detentora. Escopado ao
    dono (o lojista), para ninguém revogar enrollment de outro.
    """
    removed = delete_device_by_enrollment_id(enrollment_id, user.username)
    if not removed:
        raise HTTPException(status_code=404, detail="Enrollment não encontrado")
    core_revoked = _service.revoke_js_enrollment(enrollment_id)
    return {"revoked": True, "core_revoked": core_revoked}


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
    # Annotated + default None (em vez de ``= Query(None)``) para que o default
    # real seja None: com ``Query(None)`` o objeto de metadados vaza como valor
    # quando a funcao e chamada direto, e todo parametro ausente vira verdadeiro.
    code: Annotated[str | None, Query()] = None,
    state: Annotated[str | None, Query()] = None,
    consentId: Annotated[str | None, Query()] = None,
    status: Annotated[str | None, Query()] = None,
) -> object:
    """Recebe o redirect de volta da detentora e completa o fluxo em curso.

    Dois formatos, um por jornada:

    - ``consentId`` + ``status``: pagamento com redirecionamento. A detentora
      já mostrou valor e credor ao titular e traz aqui o que ele decidiu. Em
      ``AUTHORISED`` o pagamento é submetido; em ``REJECTED`` nada é debitado.
    - ``code`` + ``state``: cadastro de dispositivo (JSR/ITP), que continua
      passando pelo login OAuth.

    Esta é a única rota sem JWT: quem a chama é o navegador do titular, vindo
    da detentora, e um redirect não carrega o header ``Authorization``. O que a
    amarra a um usuário é o identificador na query — ``consentId`` ou ``state``
    só existem em registros criados por uma chamada já autenticada, e é de lá
    que sai o dono do pagamento/dispositivo resultante.
    """
    # -- Fluxo de pagamento com redirecionamento ---------------------------
    if consentId:
        return _complete_redirect_payment(consentId, status)

    if not code or not state:
        raise HTTPException(
            status_code=400,
            detail="Callback sem identificação: informe consentId ou code+state",
        )

    record = get_by_request_id(state) or get_by_consent_id(state)
    if not record:
        raise HTTPException(
            status_code=404, detail="Requisição de pagamento não encontrada"
        )

    try:
        payload = json.loads(record.payload or "{}")
    except json.JSONDecodeError:
        payload = {}

    if payload.get("kind") != "enrollment":
        raise HTTPException(
            status_code=400,
            detail=(
                "Pagamento com redirecionamento agora volta com consentId e "
                "status, não com code e state"
            ),
        )

    # 1. Trocar code por JWT
    jwt = _service.exchange_code(code)

    # -- Fluxo JSR/ITP: conclusão do cadastro do dispositivo ----------------
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


# Estados em que o pagamento já foi (ou está sendo) submetido: não se submete
# de novo. A detentora também barraria, mas errar aqui evita a ida à rede.
_TERMINAL_STATUSES = {"PAYMENT_SUBMITTED", "COMPLETED", "REJECTED"}


def _submit_and_record(record: ConsentRecord) -> dict:
    """Submete o pagamento na detentora e grava o desfecho no registro local.

    Chamado de dois lugares — o /callback e o webhook — que podem disparar quase
    ao mesmo tempo. Não há trava aqui de propósito: a detentora resolve a
    corrida na transição atômica AUTHORISED -> PAYMENT_SUBMITTED, e quem chega
    depois recebe 409 em vez de um segundo débito.
    """
    payment = _service.submit_aspsp_payment(record.consent_id)
    record.status = "COMPLETED"
    record.payment_id = payment.get("payment_id")
    upsert_consent(record)
    return payment


def _complete_redirect_payment(consent_id: str, status: str | None) -> object:
    """Conclui um pagamento depois que o titular decidiu na tela da detentora.

    A Iniciadora não decide nada aqui: ela repassa a decisão. Em ``REJECTED``
    nem tenta submeter; em ``AUTHORISED`` submete, e é a detentora que confere
    de novo o estado do consentimento antes de mover o dinheiro — se alguém
    chamar este callback com um status inventado, a submissão falha lá.
    """
    record = get_by_consent_id(consent_id)
    if not record:
        raise HTTPException(
            status_code=404, detail="Requisição de pagamento não encontrada"
        )

    try:
        payload = json.loads(record.payload or "{}")
    except json.JSONDecodeError:
        payload = {}
    client_redirect = payload.get("client_redirect_uri")

    if (status or "").upper() == "REJECTED":
        record.status = "REJECTED"
        upsert_consent(record)
        result = {
            "consent_id": consent_id,
            "status": "REJECTED",
            "message": "O titular recusou o pagamento. Nada foi debitado.",
        }
        if client_redirect:
            return _client_redirect(
                client_redirect, {"status": "REJECTED", "consent_id": consent_id}
            )
        return result

    payment = _submit_and_record(record)

    if client_redirect:
        return _client_redirect(
            client_redirect,
            {
                "status": "COMPLETED",
                "consent_id": consent_id,
                "payment_id": payment.get("payment_id", ""),
            },
        )

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


def verify_webhook_signature(raw_body: bytes, signature: str | None) -> bool:
    """Confere a assinatura HMAC que a detentora põe em ``x-webhook-signature``.

    Sem isso, quem descobrisse esta URL postaria ``status: AUTHORISED`` e a
    Iniciadora submeteria um pagamento que o titular nunca aprovou — ela age em
    cima do aviso. A chave é o mesmo segredo que já autentica a Iniciadora na
    detentora, então os dois lados a conhecem sem configuração nova.
    """
    if not signature:
        return False
    expected = hmac.new(
        settings.initiator_client_secret.encode("utf-8"), raw_body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


@app.post("/webhooks/consents")
async def consent_webhook(request: Request) -> dict:
    """Recebe da detentora a mudança de status de um consentimento.

    É o que fecha a jornada quando o titular aprova e fecha o navegador sem
    voltar pelo /callback: o aviso chega por aqui e o pagamento é submetido do
    mesmo jeito. Sem JWT — quem chama é a detentora, autenticada pela assinatura
    do corpo.
    """
    raw_body = await request.body()
    if not verify_webhook_signature(raw_body, request.headers.get("x-webhook-signature")):
        raise HTTPException(status_code=401, detail="Assinatura do webhook inválida")

    try:
        event = json.loads(raw_body or b"{}")
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Corpo do webhook não é JSON")

    consent_id = event.get("consentId") or ""
    record = get_by_consent_id(consent_id)
    if not record:
        # 200 de propósito: um consentimento que não é nosso não é erro da
        # detentora, e devolver 4xx só a faria tentar de novo à toa.
        return {"received": True, "known": False}

    status = (event.get("status") or "").upper()
    already_done = record.status in _TERMINAL_STATUSES

    if status == "AUTHORISED" and not already_done:
        try:
            # Em thread separada porque submeter é uma chamada HTTP bloqueante:
            # dentro de um endpoint async ela travaria o event loop inteiro
            # enquanto a detentora responde -- e a detentora chama este webhook
            # no meio de operações que a própria Iniciadora está aguardando.
            payment = await asyncio.to_thread(_submit_and_record, record)
            return {"received": True, "status": "COMPLETED", "payment_id": payment.get("payment_id")}
        except httpx.HTTPStatusError as exc:
            # 409 aqui normalmente significa que o /callback chegou primeiro e já
            # submeteu — não é falha, é corrida resolvida. Os demais erros ficam
            # registrados no status para o lojista ver.
            if exc.response.status_code != 409:
                record.status = "SUBMISSION_FAILED"
                upsert_consent(record)
            return {"received": True, "status": record.status}

    if status and status != record.status and not already_done:
        record.status = status
        upsert_consent(record)

    return {"received": True, "status": record.status}


@app.get("/payments/{identifier}")
def get_payment(identifier: str, user: UserRecord = Depends(current_user)) -> dict:
    """Consulta o status de um pagamento por payment_id ou consent_id.

    O estado vem da **detentora**, não da cópia local: o registro daqui só sabe
    o que passou por esta aplicação, e as duas visões divergem em silêncio
    sempre que o titular aprova e não volta pelo /callback. A consulta
    reconcilia e persiste o que o core respondeu.

    Se o core não responder, devolve a cópia local marcada como tal — um lojista
    com resposta velha e avisado disso é melhor que um 502.

    Só enxerga os pagamentos do próprio usuário: o identificador de outro
    titular responde 404, sem revelar que ele existe.
    """
    record = get_by_payment_id(identifier, user.username) or get_by_consent_id(
        identifier, user.username
    )
    if not record:
        raise HTTPException(status_code=404, detail="Pagamento não encontrado")

    source = "core"
    try:
        consent = _service.get_aspsp_consent(record.consent_id)
    except httpx.HTTPError:
        consent = {}
        source = "local"

    core_status = consent.get("status")
    if core_status and (
        core_status != record.status or consent.get("paymentId") != record.payment_id
    ):
        record.status = core_status
        record.payment_id = consent.get("paymentId") or record.payment_id
        upsert_consent(record)

    return {
        "consent_id": record.consent_id,
        "request_id": record.request_id,
        "status": record.status,
        "payment_id": record.payment_id,
        "created_at": record.created_at.isoformat(),
        "source": source,
        "consent": consent or None,
    }


@app.get("/payments/{identifier}/events")
def get_payment_events(
    identifier: str, user: UserRecord = Depends(current_user)
) -> dict:
    """Trilha do consentimento na detentora: o que foi tentado, por quem, e o
    que passou. Inclui as tentativas recusadas, que não deixam marca no status.
    """
    record = get_by_payment_id(identifier, user.username) or get_by_consent_id(
        identifier, user.username
    )
    if not record:
        raise HTTPException(status_code=404, detail="Pagamento não encontrado")

    return _service.get_aspsp_consent_events(record.consent_id)
