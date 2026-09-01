"""Serviço da Iniciadora: comunicação com o core-banking (detentora).

Em desenvolvimento, chama o core diretamente (paths /v1/...).
Em produção, chama via gateway proxy Sensedia (paths /open-banking/...).
"""

import uuid

import httpx

from .config import settings


def _uuid() -> str:
    return str(uuid.uuid4())


class CoreBankingService:
    """Encapsula as chamadas ao core-banking (OAuth + ASPSP + accounts)."""

    def __init__(self) -> None:
        self.use_proxy = settings.use_proxy
        self.base_url = (
            settings.gateway_base_url.rstrip("/")
            if self.use_proxy
            else settings.core_base_url.rstrip("/")
        )

    # -- Helpers de construção de URL --------------------------------------

    def _auth_path(self, suffix: str) -> str:
        if self.use_proxy:
            return f"{self.base_url}/open-banking/auth{suffix}"
        return f"{self.base_url}/v1/auth{suffix}"

    def _accounts_path(self) -> str:
        if self.use_proxy:
            return f"{self.base_url}/open-banking/accounts"
        return f"{self.base_url}/v1/me/accounts"

    def _consents_path(self) -> str:
        if self.use_proxy:
            return f"{self.base_url}/{settings.pisp_path}/payments/v5/consents"
        return f"{self.base_url}/v1/aspsp/payments/consents"

    def _payments_path(self) -> str:
        if self.use_proxy:
            return f"{self.base_url}/{settings.pisp_path}/payments/v5/pix/payments"
        return f"{self.base_url}/v1/aspsp/payments"

    def _payment_status_path(self, payment_id: str) -> str:
        if self.use_proxy:
            return f"{self.base_url}/{settings.pisp_path}/payments/v5/pix/payments/{payment_id}"
        return f"{self.base_url}/v1/aspsp/payments/{payment_id}"

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

    # -- 4. ASPSP: criar consentimento --------------------------------------

    def create_aspsp_consent(self, jwt: str, payload: dict) -> str:
        url = self._consents_path()
        body = self._build_consent_body(payload)
        with httpx.Client(timeout=30.0) as client:
            resp = client.post(
                url,
                json=body,
                headers={**self._headers(jwt), "x-idempotency-key": _uuid()},
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
        return consent_id

    # -- 5. ASPSP: submeter o pagamento -------------------------------------

    def submit_aspsp_payment(self, jwt: str, consent_id: str) -> dict:
        url = self._payments_path()
        body = {"consentId": consent_id}
        with httpx.Client(timeout=30.0) as client:
            resp = client.post(
                url,
                json=body,
                headers={**self._headers(jwt), "x-idempotency-key": _uuid()},
            )
            resp.raise_for_status()
        data = resp.json()
        return {
            "payment_id": data.get("paymentId") or data.get("payment_id", ""),
            "consent_id": consent_id,
            "status": data.get("status", ""),
        }

    # -- 6. ASPSP: consultar status -----------------------------------------

    def get_aspsp_status(self, jwt: str, identifier: str) -> dict:
        url = self._payment_status_path(identifier)
        with httpx.Client(timeout=30.0) as client:
            resp = client.get(url, headers=self._headers(jwt))
            resp.raise_for_status()
        return resp.json()

    # -- Montagem do payload de consentimento -------------------------------

    def _build_consent_body(self, payload: dict) -> dict:
        """Monta o body do consentimento conforme o modo (dev/prod)."""
        if self.use_proxy:
            # Vocabulary Open Finance formal (PISP v5)
            return {
                "redirect_uri": settings.callback_url,
                "authorisation_server": {
                    "authorisation_server_id": settings.authorisation_server_id,
                    "organisation_id": settings.organisation_id,
                },
                "creditor": {
                    "person_type": "PESSOA_NATURAL",
                    "cpf_cnpj": payload["creditor_cpf_cnpj"],
                    "name": payload["creditor_name"],
                },
                "payment": {
                    "type": "PIX",
                    "purpose": "IMMEDIATE",
                    "date": "2026-12-22",
                    "currency": payload.get("currency", "BRL"),
                    "amount": payload["amount"],
                    "details": {
                        "local_instrument": "DICT",
                        "proxy": payload["creditor_key"]["value"],
                        "creditor_account": {
                            "ispb": "00000000",
                            "issuer": "0001",
                            "number": payload["creditor_key"]["value"],
                            "account_type": "CACC",
                        },
                    },
                },
                "debtor_account": {
                    "ispb": "00000000",
                    "issuer": "0001",
                    "number": payload.get("debtor_account_number", ""),
                    "account_type": "CACC",
                },
                "remittance_information": "Pagamento via Open Finance",
            }

        # Formato do core (dev): /v1/aspsp/payments/consents
        return {
            "accountId": payload["account_id"],
            "amount": payload["amount"],
            "creditorName": payload["creditor_name"],
            "creditorDocument": payload.get("creditor_cpf_cnpj"),
            "creditorKey": {
                "type": payload["creditor_key"]["type"],
                "value": payload["creditor_key"]["value"],
            },
            "description": payload.get("description", "Pagamento via Open Finance"),
        }
