"""Modelos de dados da Iniciadora (PISP v5)."""

from typing import Literal, Optional

from pydantic import BaseModel, Field

PersonType = Literal["PESSOA_NATURAL", "PESSOA_JURIDICA"]
PixKeyType = Literal["CPF", "CNPJ", "EMAIL", "PHONE", "EVP"]


# ---------------------------------------------------------------------------
# Payload de entrada do endpoint POST /payments (dados simplificados da demo)
# ---------------------------------------------------------------------------


class PaymentInitiationRequest(BaseModel):
    """Dados mínimos para iniciar um pagamento PIX (via Open Finance)."""

    amount: str = Field(..., description="Valor em BRL, ex: '100.00'")
    currency: str = "BRL"

    creditor_name: str
    creditor_cpf_cnpj: str
    creditor_key_type: PixKeyType
    creditor_key_value: str

    debtor_cpf: str = Field(..., description="CPF do pagador (titular da conta)")
    debtor_account_number: str = Field(..., description="Número da conta de débito")


# ---------------------------------------------------------------------------
# Corpo do consentimento PISP v5 (conforme vocabulary Open Finance)
# ---------------------------------------------------------------------------


class CreditorAccount(BaseModel):
    ispb: str = "00000000"
    issuer: str = "0001"
    number: str
    account_type: str = "CACC"


class Creditor(BaseModel):
    person_type: PersonType = "PESSOA_NATURAL"
    cpf_cnpj: str
    name: str


class PaymentDetails(BaseModel):
    local_instrument: str = "DICT"
    proxy: str
    creditor_account: CreditorAccount


class ScheduleSingle(BaseModel):
    date: str


class Schedule(BaseModel):
    single: ScheduleSingle


class Payment(BaseModel):
    type: str = "PIX"
    purpose: str = "IMMEDIATE"
    date: str
    currency: str = "BRL"
    amount: str
    details: PaymentDetails


class DebtorAccount(BaseModel):
    ispb: str = "00000000"
    issuer: str = "0001"
    number: str
    account_type: str = "CACC"


class BusinessEntity(BaseModel):
    document: dict = Field(
        default_factory=lambda: {"identification": "", "rel": "CNPJ"}
    )


class LoggedUser(BaseModel):
    document: dict = Field(default_factory=lambda: {"identification": "", "rel": "CPF"})


class AdditionalInfo(BaseModel):
    key: str = "Nome"
    value: str = ""


class AuthorisationServer(BaseModel):
    authorisation_server_id: str
    organisation_id: str


class ConsentRequest(BaseModel):
    """Corpo do POST /open-banking/pisp/payments/v5/consents."""

    redirect_uri: Optional[str] = None
    additional_infos: list[AdditionalInfo] = Field(default_factory=list)
    authorisation_server: AuthorisationServer
    business_entity: BusinessEntity = Field(default_factory=BusinessEntity)
    logged_user: LoggedUser = Field(default_factory=LoggedUser)
    creditor: Creditor
    payment: Payment
    debtor_account: DebtorAccount
    remittance_information: str = "Pagamento via Open Finance"


# ---------------------------------------------------------------------------
# Respostas / outros
# ---------------------------------------------------------------------------


class ConsentCreated(BaseModel):
    consent_id: str
    redirect_uri: str
    request_id: str


class PaymentResult(BaseModel):
    payment_id: str
    consent_id: str
    status: str
