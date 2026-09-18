"""Serviço da Iniciadora: comunicação com o core-banking (detentora).

Em desenvolvimento, chama o core diretamente (paths /v1/...).
Em produção, chama via gateway proxy Sensedia (paths /open-banking/...).
"""

import uuid
import urllib.parse

import httpx

from .assertion import sign_fido_assertion
from .config import settings


def _uuid() -> str:
    return str(uuid.uuid4())


class CoreBankingService:
    """Encapsula as chamadas ao core-banking (OAuth + ASPSP + accounts)."""

    def __init__(self) -> None:
        self.base_url = settings.core_base_url.rstrip("/")

    # -- Helpers de construção de URL --------------------------------------

    def _auth_path(self, suffix: str) -> str:
        return f"{self.base_url}/v1/auth{suffix}"

    def _accounts_path(self) -> str:
        return f"{self.base_url}/v1/me/accounts"

    def _consents_path(self) -> str:
        return f"{self.base_url}/v1/aspsp/payments/consents"

    def _payments_path(self) -> str:
        return f"{self.base_url}/v1/aspsp/payments"

    def _consent_path(self, consent_id: str) -> str:
        return f"{self.base_url}/v1/aspsp/payments/consents/{consent_id}"

    def _js_path(self, suffix: str) -> str:
        # Os endpoints JSR (ITP + PISP) usam os paths /open-banking/... expostos
        # diretamente pelo core-banking.
        return f"{self.base_url}{suffix}"

    # -- Headers -----------------------------------------------------------

    def _headers(self, jwt: str | None = None) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if jwt:
            headers["Authorization"] = f"Bearer {jwt}"
            headers["x-Authorization"] = jwt
        return headers

    # -- 1. OAuth: criar auth request (inicia o redirect) ------------------

    def create_auth_request(self, redirect_uri: str) -> dict:
        url = self._auth_path("/authorize")
        body = {"redirect_uri": redirect_uri}
        with httpx.Client(timeout=30.0) as client:
            resp = client.post(url, json=body, headers=self._headers())
            resp.raise_for_status()
        data = resp.json()
        return {
            "request_id": data.get("request_id", ""),
            "login_url": data.get("login_url", ""),
        }

    # -- 2. OAuth: trocar code por access_token (JWT) ----------------------

    def exchange_code(self, code: str) -> str:
        url = self._auth_path("/token")
        body = {"code": code}
        with httpx.Client(timeout=30.0) as client:
            resp = client.post(url, json=body, headers=self._headers())
            resp.raise_for_status()
        data = resp.json()
        token = data.get("access_token") or data.get("accessToken") or ""
        if not token:
            raise RuntimeError("Resposta OAuth sem access_token")
        return token

    # -- 3. Accounts: listar contas do usuário ------------------------------

    def list_accounts(self, jwt: str) -> list[dict]:
        url = self._accounts_path()
        with httpx.Client(timeout=30.0) as client:
            resp = client.get(url, headers=self._headers(jwt))
            resp.raise_for_status()
        data = resp.json()
        if isinstance(data, list):
            return data
        return data.get("accounts", [])

    # -- 4. ASPSP: criar consentimento (pendente de aprovacao) --------------

    def create_aspsp_consent(self, payload: dict) -> dict:
        """Cria o consentimento na Detentora e colhe a URL de aprovacao.

        E a primeira coisa que acontece na jornada com redirecionamento: o
        consentimento nasce ``AWAITING_AUTHORISATION``, sem titular e sem conta
        -- nesta altura a Iniciadora nao sabe quem vai pagar. Quem preenche
        esses dados e o proprio titular, na ``authorisation_url``.

        Autentica com ``x-initiator-key``, e nao com JWT de usuario: a chamada e
        de serviço para serviço, feita antes de existir qualquer sessao do
        titular.
        """
        url = self._consents_path()
        body = self._build_consent_body(payload)
        with httpx.Client(timeout=30.0) as client:
            resp = client.post(
                url,
                json=body,
                headers={**self._initiator_headers(), "x-idempotency-key": _uuid()},
            )
            resp.raise_for_status()
        data = resp.json()
        consent_id = (
            data.get("consentId")
            or data.get("consent_id")
            or resp.headers.get("x-pisp-consent-id", "")
        )
        if not consent_id:
            raise RuntimeError("Resposta sem consent_id")
        return {
            "consent_id": consent_id,
            "status": data.get("status", "AWAITING_AUTHORISATION"),
            "authorisation_url": (
                data.get("authorisationUrl")
                or data.get("links", {}).get("redirect", "")
            ),
        }

    # -- 5. ASPSP: submeter o pagamento -------------------------------------

    def submit_aspsp_payment(self, consent_id: str) -> dict:
        """Submete o pagamento de um consentimento ja aprovado.

        A Detentora recusa (409 ``CONSENT_NOT_AUTHORISED``) se o titular ainda
        nao confirmou ou se recusou -- e essa recusa que garante que nenhum
        pagamento sai sem aprovacao, independente do que a Iniciadora tente.
        """
        url = self._payments_path()
        body = {"consentId": consent_id}
        with httpx.Client(timeout=30.0) as client:
            resp = client.post(
                url,
                json=body,
                headers={**self._initiator_headers(), "x-idempotency-key": _uuid()},
            )
            resp.raise_for_status()
        data = resp.json()
        return {
            "payment_id": data.get("paymentId") or data.get("payment_id", ""),
            "consent_id": consent_id,
            "status": data.get("status", ""),
        }

    # -- 6. ASPSP: consultar o consentimento --------------------------------

    def get_aspsp_consent(self, consent_id: str) -> dict:
        url = self._consent_path(consent_id)
        with httpx.Client(timeout=30.0) as client:
            resp = client.get(url, headers=self._initiator_headers())
            resp.raise_for_status()
        return resp.json()

    # -- Montagem do payload de consentimento -------------------------------

    def _build_consent_body(self, payload: dict) -> dict:
        """Monta o body do consentimento no formato do core-banking.

        Sem ``accountId``: a conta de debito e escolhida pelo titular na tela de
        aprovacao. ``redirect_uri`` e para onde a Detentora devolve o navegador
        com o desfecho.
        """
        body = {
            "amount": payload["amount"],
            "creditorName": payload["creditor_name"],
            "creditorDocument": payload.get("creditor_cpf_cnpj"),
            "creditorKey": {
                "type": payload["creditor_key"]["type"],
                "value": payload["creditor_key"]["value"],
            },
            "description": payload.get("description", "Pagamento via Open Finance"),
            "redirect_uri": payload["redirect_uri"],
        }
        if payload.get("debtor_cpf"):
            body["debtorDocument"] = payload["debtor_cpf"]
        if payload.get("webhook_uri"):
            body["webhook_uri"] = payload["webhook_uri"]
        return body

    # -- 7. ASPSP: trilha de eventos do consentimento -----------------------

    def get_aspsp_consent_events(self, consent_id: str) -> dict:
        """Histórico do consentimento na detentora: o que foi tentado e o que
        passou. Serve para auditar a jornada sem abrir o banco do core."""
        url = f"{self._consent_path(consent_id)}/events"
        with httpx.Client(timeout=30.0) as client:
            resp = client.get(url, headers=self._initiator_headers())
            resp.raise_for_status()
        return resp.json()

    # ------------------------------------------------------------------
    # Jornada JSR (FIDO2 simplificado) — ITP enrollment + PISP JSR
    # ------------------------------------------------------------------

    def _initiator_headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if settings.initiator_client_secret:
            headers["x-initiator-key"] = settings.initiator_client_secret
        return headers

    def revoke_js_enrollment(self, enrollment_id: str) -> bool:
        """Best-effort: pede à detentora para revogar o enrollment (JSR/ITP).

        Nem toda detentora expõe DELETE do enrollment, então não propaga erro —
        a fonte que impede novos pagamentos é a revogação local (o vínculo
        guardado na Iniciadora). Devolve True se a detentora aceitou.
        """
        url = self._js_path(f"/open-banking/itp/v2/enrollments/{enrollment_id}")
        try:
            with httpx.Client(timeout=30.0) as client:
                resp = client.delete(url, headers=self._initiator_headers())
            return resp.status_code < 400
        except httpx.HTTPError:
            return False

    def create_js_enrollment(self, redirect_uri: str) -> dict:
        url = self._js_path("/open-banking/itp/v2/enrollments")
        body = {"redirect_uri": redirect_uri}
        with httpx.Client(timeout=30.0) as client:
            resp = client.post(url, json=body, headers=self._initiator_headers())
            resp.raise_for_status()
        return {
            "enrollment_id": resp.headers.get("x-itp-enrollment-id", ""),
            "redirect_uri": resp.json().get("redirect_uri", ""),
            "request_id": resp.json().get("request_id", ""),
            "fido_registration_options": resp.json().get("fidoRegistrationOptions", {}),
        }

    def account_holder_confirmed_js(
        self, enrollment_id: str, username: str, account_number: str = ""
    ) -> dict:
        """Confirma o titular do enrollment (JSR/ITP) e colhe code+state.

        Faz PATCH em /enrollment-supports/v2/.../account-holder-confirmed. A
        detentora responde com um header ``Location`` apontando para o nosso
        próprio callback com ``code`` e ``state`` — basta extraí-los da URL
        (não seguimos o redirect, pois isso recursaria no /callback).
        """
        url = self._js_path(
            "/open-banking/enrollment-supports/v2/"
            f"enrollment-supports/{enrollment_id}/account-holder-confirmed"
        )
        body = {
            "data": {
                "transactionLimit": "40.00",
                "dailyLimit": "1000.00",
                "debtorAccount": {
                    "number": account_number,
                    "accountType": "CACC",
                    "ibgeTownCode": "1234567",
                },
                "fidoUser": {"name": username, "displayName": username},
            }
        }
        with httpx.Client(timeout=30.0) as client:
            resp = client.patch(url, json=body, headers=self._initiator_headers())
            resp.raise_for_status()
        location = resp.headers.get("location", "")
        parsed = urllib.parse.urlsplit(location)
        query = urllib.parse.parse_qs(parsed.query)
        return {
            "authorization_code": query.get("code", [""])[0],
            "request_id": query.get("state", [""])[0],
        }

    def confirm_js_enrollment(
        self, enrollment_id: str, authorization_code: str, request_id: str
    ) -> dict:
        url = self._js_path("/open-banking/itp/v2/enrollments/confirmations")
        body = {"authorizationCode": authorization_code, "requestId": request_id}
        with httpx.Client(timeout=30.0) as client:
            resp = client.post(url, json=body, headers=self._initiator_headers())
            resp.raise_for_status()
        return resp.json()

    def register_js_fido(self, enrollment_id: str, fido_response: dict) -> None:
        url = self._js_path(
            f"/open-banking/itp/v2/enrollments/{enrollment_id}/fido-registration"
        )
        with httpx.Client(timeout=30.0) as client:
            resp = client.post(
                url, json=fido_response, headers=self._initiator_headers()
            )
            resp.raise_for_status()

    def create_js_consent(self, payload: dict) -> dict:
        url = self._js_path("/open-banking/pisp/payments/v5/jsr/consents")
        with httpx.Client(timeout=30.0) as client:
            resp = client.post(url, json=payload, headers=self._initiator_headers())
            resp.raise_for_status()
        data = resp.json()
        return {
            "consent_id": resp.headers.get(
                "x-pisp-consent-id", data.get("consentId", "")
            ),
            "fido_challenge": data.get("fidoChallenge", ""),
        }

    def authorise_js_consent(
        self, consent_id: str, credential_id: str, challenge: str
    ) -> None:
        """Autoriza o consentimento provando posse da credencial.

        A Detentora exige os três campos: o challenge deixou de ser opcional e a
        assinatura amarra a autorização a este consentimento e a esta
        credencial. Ver ``assertion.py`` para o que ela prova.
        """
        if not challenge:
            raise ValueError("challenge é obrigatório para autorizar o consentimento")

        url = self._js_path(f"/open-banking/itp/v2/consents/{consent_id}/authorise")
        body = {
            "credentialId": credential_id,
            "challenge": challenge,
            "signature": sign_fido_assertion(
                settings.initiator_client_secret, consent_id, credential_id, challenge
            ),
        }
        headers = {**self._initiator_headers(), "x-bcb-nfc": "true"}
        with httpx.Client(timeout=30.0) as client:
            resp = client.post(url, json=body, headers=headers)
            resp.raise_for_status()

    def initiate_js_payment(self, consent_id: str) -> dict:
        url = self._js_path("/open-banking/pisp/payments/v5/jsr/pix/payments")
        body = {
            "consentId": consent_id,
            "authorisationFlow": "FIDO_FLOW",
            "endToEndIds": [_uuid().replace("-", "")],
        }
        with httpx.Client(timeout=30.0) as client:
            resp = client.post(url, json=body, headers=self._initiator_headers())
            resp.raise_for_status()
        return {"payment_id": resp.json().get("paymentId", "")}
