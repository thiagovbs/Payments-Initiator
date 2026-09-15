# Payment Initiator (Iniciadora de Pagamento)

Aplicação **Iniciadora de Pagamento (PISP)** Open Finance Brasil construída em Python/FastAPI. Orquestra a iniciação de pagamentos PIX junto à detentora (core-banking), tanto na **jornada com redirect** quanto na **jornada JSR** (pagamento sem redirect com dispositivo vinculado via FIDO2).

> Este serviço é o lado "Iniciadora" da demo Open Finance. O backend da detentora é o [Banking API](../banking-api-render-rds/banking-api/README.md) (core-banking).

## Funcionalidades

- **Pagamento com redirect (PISP v5)**: cria consentimento, autentica o usuário na detentora via OAuth e efetua o PIX.
- **Jornada JSR**: cadastro de dispositivo (ITP enrollment + FIDO2) e pagamento PIX **sem redirect**.
- **Modo dual**: chama o core-banking **direto** (desenvolvimento, paths `/v1/...`) ou via **gateway Sensedia** (produção, paths `/open-banking/...`).
- **Persistência local** (SQLite via SQLModel) de consentimentos e dispositivos vinculados.
- Proxy dos paths formais Open Finance (auth, contas, consentimentos PISP v5).
- Documentação OpenAPI gerada automaticamente (FastAPI).

## Stack

- Python 3.11+
- FastAPI + Uvicorn
- httpx (chamadas ao core/gateway)
- Pydantic / pydantic-settings
- SQLModel + SQLite (`initiator.db`)
- uv (gestão de dependências)

## Requisitos

- Python 3.11+
- [uv](https://docs.astral.sh/uv/) (ou pip + virtualenv)
- Core-banking rodando em `http://localhost:3000` (para modo dev)

## Configuração do ambiente

Copie o `.env.example` para `.env` e ajuste:

```bash
cp .env.example .env
```

| Variável | Descrição | Padrão |
|---|---|---|
| `INITIATOR_CLIENT_ID` | Client ID da iniciadora (produção) | — |
| `INITIATOR_CLIENT_SECRET` | Secret da iniciadora; **deve igualar `INITIATOR_SERVICE_SECRET` do core** (header `x-initiator-key`) | `initiator-dev-secret` |
| `CORE_BASE_URL` | URL do core-banking (chamada direta) | `http://localhost:3000` |
| `CALLBACK_URL` | URL pública desta iniciadora para receber o redirect da detentora | `http://localhost:8100/callback` |
| `ORGANISATION_ID` | Organização (produção) | — |
| `AUTHORISATION_SERVER_ID` | Servidor de autorização (produção) | — |
| `DATABASE_URL` | Banco local (SQLite). O diretório é criado automaticamente | `sqlite:///./data/initiator.db` |
| `PORT` | Porta do servidor FastAPI | `8100` |

## Instalação e execução

Com `uv`:

```bash
uv sync
uv run uvicorn app.main:app --host 0.0.0.0 --port 8100
```

Com `pip` + virtualenv:

```bash
python -m venv .venv
.\.venv\Scripts\activate        # Windows
source .venv/bin/activate       # Linux/macOS
pip install -r <requirements>   # ou: uv pip install -e .
uvicorn app.main:app --host 0.0.0.0 --port 8100
```

A API sobe em `http://localhost:8100`.

- Docs interativas (Swagger UI): `http://localhost:8100/docs`
- OpenAPI JSON: `http://localhost:8100/openapi.json`

> Obs.: a iniciadora cria o arquivo `initiator.db` (SQLite) no diretório de trabalho para persistir consentimentos e dispositivos.


## Testes

```bash
uv run --group dev pytest
```

Entre eles, um vetor fixo que trava a paridade da assinatura FIDO com a
Detentora: o mesmo HMAC é calculado nos dois repositórios, e mudar o formato da
mensagem em apenas um dos lados quebraria a jornada JSR em runtime.

## Comunicação com o core-banking

A iniciadora chama o core-banking **diretamente** em `CORE_BASE_URL`, usando os paths `/v1/...` (auth, contas, consents ASPSP) e `/open-banking/...` (rotas JSR). Os paths são idênticos em qualquer ambiente.

Nas chamadas às rotas JSR do core, a iniciadora envia o header `x-initiator-key` com o valor de `INITIATOR_CLIENT_SECRET`.

## Endpoints

| Método | Endpoint | Descrição |
|---|---|---|
| POST | `/payments` | Inicia pagamento PIX com redirect na detentora |
| GET | `/payments/{identifier}` | Consulta status de um pagamento (por `payment_id` ou `consent_id`) |
| POST | `/enrollments` | Inicia o cadastro de dispositivo (JSR/ITP) na detentora |
| GET | `/callback` | Recebe o redirect da detentora e conclui o fluxo (enrollment ou pagamento) |
| POST | `/payments/jsr` | Inicia pagamento PIX sem redirect (JSR) usando o dispositivo vinculado |

### Proxy dos paths formais Open Finance

| Método | Endpoint | Descrição |
|---|---|---|
| POST | `/open-banking/auth/authorize` | Inicia o fluxo OAuth da detentora |
| POST | `/open-banking/auth/token` | Troca `code` por `access_token` |
| GET | `/open-banking/accounts` | Lista contas do usuário autenticado |
| POST | `/open-banking/pisp/payments/v5/consents` | Cria consentimento de pagamento (PISP v5) |
| POST | `/open-banking/pisp/payments/v5/pix/payments` | Inicia o pagamento PIX após autorização |
| GET | `/open-banking/pisp/payments/v5/pix/payments/{paymentId}` | Consulta status do pagamento PIX |

## Jornadas

### 1. Pagamento com redirect (PISP v5)

1. `POST /payments` → retorna `request_id` + `login_url`.
2. Abra `login_url`; o usuário autentica na detentora.
3. A detentora redireciona para `CALLBACK_URL?code=...&state=...`.
4. `GET /callback` troca o `code` por JWT, resolve a conta, cria o consentimento ASPSP e submete o pagamento.
5. Retorna `payment_id`, `consent_id`, `status`.

### 2. Jornada JSR (pagamento sem redirect)

**Cadastro do dispositivo (uma vez):**

1. `POST /enrollments` → retorna `enrollment_id` + `login_url`.
2. Usuário autentica na detentora; o core redireciona para o callback.
3. `GET /callback` (kind=enrollment) confirma o titular, registra o FIDO, resolve a conta e **persiste o dispositivo** como `REGISTERED` (com `credential_id` + `account_id`).
4. Retorna `credential_id` e `account_id`.

**Pagamento JSR:**

1. `POST /payments/jsr` com os dados do crédito.
2. Se houver um dispositivo `REGISTERED`, cria o consentimento JSR, autoriza com a credencial FIDO e inicia o PIX → retorna `payment_id`.
3. Se **não** houver dispositivo, retorna `HTTP 400` com `need_enrollment: true` e o `login_url` para cadastro.

## Persistência local (SQLite)

Via `SQLModel`, a iniciadora mantém em `initiator.db`:

- **ConsentRecord** — consentimentos/pagamentos em andamento (`consent_id`, `request_id`, `status`, `payment_id`, `payload`).
- **DeviceRecord** — dispositivos vinculados (`enrollment_id`, `credential_id`, `username`, `account_id`, `status` `PENDING`/`REGISTERED`).

O dispositivo `REGISTERED` é o que habilita a jornada JSR (pagamento sem redirect).

## Considerações

- Esta é uma **demo educacional/técnica**: o FIDO é simplificado (mock) e a validação criptográfica WebAuthn não é realizada.
- A iniciadora comunica-se diretamente com o core-banking em `CORE_BASE_URL`; eventuais camadas de gateway/roteamento ficam transparentes para a aplicação.
- O `INITIATOR_CLIENT_SECRET` deve ser tratado como segredo e nunca versionado.
