# Payment Initiator (Iniciadora de Pagamento)

Aplicação **Iniciadora de Pagamento (PISP)** Open Finance Brasil construída em Python/FastAPI. Orquestra a iniciação de pagamentos PIX junto à detentora (core-banking), tanto na **jornada com redirect** quanto na **jornada JSR** (pagamento sem redirect com dispositivo vinculado via FIDO2).

> Este serviço é o lado "Iniciadora" da demo Open Finance. O backend da detentora é o [Banking API](../banking-api-render-rds/banking-api/README.md) (core-banking).

## Funcionalidades

- **Multiusuário com JWT**: login com usuário e senha pré-cadastrados (`POST /auth/login`) e token `Bearer` obrigatório nas demais rotas; cada usuário só enxerga os próprios pagamentos e dispositivos.
- **Pagamento com redirect (PISP v5)**: cria consentimento, autentica o usuário na detentora via OAuth e efetua o PIX.
- **Jornada JSR**: cadastro de dispositivo (ITP enrollment + FIDO2) e pagamento PIX **sem redirect**.
- **Persistência local** (SQLite via SQLModel) de usuários, consentimentos e dispositivos vinculados.
- **Erros do core repassados**: a recusa da detentora (`INSUFFICIENT_BALANCE`, `PIX_KEY_NOT_FOUND`, `ENROLLMENT_ACCOUNT_MISMATCH`) chega ao cliente com o motivo original, em vez de virar um 500 opaco.
- Documentação OpenAPI gerada automaticamente (FastAPI) e contrato do gateway em [`openapi Sensedia.yaml`](openapi%20Sensedia.yaml).

## Stack

- Python 3.11+
- FastAPI + Uvicorn
- httpx (chamadas ao core/gateway)
- Pydantic / pydantic-settings
- SQLModel + SQLite (`data/initiator.db`)
- PyJWT (access token HS256) + PBKDF2-HMAC-SHA256 (senhas)
- uv (gestão de dependências)

## Requisitos

- Python 3.11+
- [uv](https://docs.astral.sh/uv/) (ou pip + virtualenv)
- Core-banking acessível no endereço de `CORE_BASE_URL` (padrão `http://localhost:3000`)

## Configuração do ambiente

Copie o `.env.example` para `.env` e ajuste:

```bash
cp .env.example .env
```

| Variável | Descrição | Padrão |
|---|---|---|
| `INITIATOR_CLIENT_ID` | Client ID da iniciadora (produção) | — |
| `INITIATOR_CLIENT_SECRET` | Secret da iniciadora; **deve igualar `INITIATOR_SERVICE_SECRET` do core** (header `x-initiator-key` e assinatura da asserção FIDO) | — |
| `CORE_BASE_URL` | URL do core-banking (chamada direta) | `http://localhost:3000` |
| `CALLBACK_URL` | URL pública desta iniciadora para receber o redirect da detentora | `http://localhost:8100/callback` |
| `ORGANISATION_ID` | Organização (produção) | — |
| `AUTHORISATION_SERVER_ID` | Servidor de autorização (produção) | — |
| `DATABASE_URL` | Banco local (SQLite). O diretório é criado automaticamente | `sqlite:///./data/initiator.db` |
| `PORT` | Porta do servidor FastAPI | `8100` |
| `JWT_SECRET` | Segredo que assina os access tokens. **Defina um próprio** (≥ 32 caracteres) | segredo de desenvolvimento (a aplicação avisa no log) |
| `JWT_EXPIRES_MINUTES` | Validade do token, em minutos | `60` |
| `INITIATOR_USERS` | Usuários pré-cadastrados, semeados a cada start | — |

Gerando um segredo:

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

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
pip install fastapi "uvicorn[standard]" httpx pydantic pydantic-settings sqlmodel python-dotenv pyjwt
uvicorn app.main:app --host 0.0.0.0 --port 8100
```

A API sobe em `http://localhost:8100`.

- Docs interativas (Swagger UI): `http://localhost:8100/docs` — use **Authorize** para colar o token
- OpenAPI JSON: `http://localhost:8100/openapi.json`

> Obs.: a iniciadora cria `data/initiator.db` (SQLite) para persistir usuários, consentimentos e dispositivos. O diretório é criado automaticamente.

### Docker

```bash
docker compose up --build
```

O compose lê o `.env` local e publica a API em `http://localhost:3200` (porta 8100 dentro do container). O volume `initiator-data` persiste **apenas** `/app/data` — montá-lo em `/app` sombrearia o código e o virtualenv da imagem, e a partir da segunda subida o container rodaria a versão antiga.

## Autenticação (JWT)

A Iniciadora atende vários titulares, e cada chamada precisa dizer **quem** está pagando: é o usuário autenticado que amarra consentimentos e dispositivos vinculados.

### Usuários pré-cadastrados

Não há cadastro aberto. Os usuários vêm de `INITIATOR_USERS` e são gravados no banco a cada start — trocar uma senha ali passa a valer no próximo start, sem apagar o banco. Dois formatos:

```bash
# curto
INITIATOR_USERS=alice:alice123,bruno:bruno123

# JSON (permite full_name)
INITIATOR_USERS=[{"username": "alice", "password": "alice123", "full_name": "Alice Silva"}]
```

As senhas são guardadas como hash PBKDF2-HMAC-SHA256 com salt por usuário — o banco não contém a senha em claro.

### Obtendo e usando o token

```bash
curl -s -X POST http://localhost:8100/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"username": "alice", "password": "alice123"}'
```

```json
{
  "access_token": "eyJhbGciOiJIUzI1NiIs...",
  "token_type": "bearer",
  "expires_in": 3600,
  "expires_at": "2026-09-17T12:43:24+00:00",
  "username": "alice"
}
```

Nas demais rotas, envie o token:

```bash
curl -s http://localhost:8100/enrollments -H "Authorization: Bearer $TOKEN"
```

No Swagger UI (`/docs`), use o botão **Authorize** e cole o token.

### O que o token garante

- **Escopo por titular**: pagamento JSR só aceita `enrollment_id` de dispositivo do próprio usuário — o de outro titular responde `404`, como se não existisse. `GET /payments/{identifier}` idem.
- **Revogação imediata**: o usuário é relido do banco a cada chamada; desabilitá-lo (`disabled`) vale na hora, sem esperar o token expirar.
- **Usuário inexistente e senha errada** devolvem o mesmo `401`, para não revelar quais usuários existem.

### A exceção: `GET /callback`

`GET /callback` é a única rota **sem** token: quem a chama é o navegador do titular, vindo da detentora, e um redirect não carrega o header `Authorization`. O que a amarra a um usuário é o `state` — o `request_id` que só existe num registro criado por uma chamada já autenticada, e de onde sai o dono do dispositivo/pagamento resultante.

## Endpoints

Todas as rotas exigem `Authorization: Bearer <token>`, exceto `POST /auth/login` e `GET /callback`.

| Método | Endpoint | Descrição |
|---|---|---|
| POST | `/auth/login` | Troca usuário e senha por um access token JWT |
| GET | `/auth/me` | Devolve o usuário do token (confere se ele ainda vale) |
| POST | `/payments` | Inicia pagamento PIX com redirect na detentora |
| GET | `/payments/{identifier}` | Consulta status de um pagamento do próprio usuário (por `payment_id` ou `consent_id`) |
| POST | `/enrollments` | Inicia o cadastro de dispositivo (JSR/ITP) na detentora |
| GET | `/enrollments` | Lista os dispositivos vinculados do usuário (de onde sai o `enrollment_id`) |
| GET | `/callback` | Recebe o redirect da detentora e conclui o fluxo (enrollment ou pagamento) |
| POST | `/payments/jsr` | Inicia pagamento PIX sem redirect (JSR) com o dispositivo indicado em `enrollment_id` |

Esses são todos os endpoints expostos. Os paths formais Open Finance
(`/open-banking/itp/v2/enrollments`, `/open-banking/pisp/payments/v5/jsr/...`)
são **consumidos** na detentora por [`app/initiator.py`](app/initiator.py) — não
são reexpostos aqui.

## Jornadas

Em ambas, o passo zero é o mesmo: `POST /auth/login` e o token no header
`Authorization` de toda chamada (menos o `GET /callback`, que vem do navegador).

### 1. Pagamento com redirect (PISP v5)

1. `POST /payments` → retorna `request_id` + `login_url`.

   ```bash
   curl -s -X POST http://localhost:8100/payments \
     -H "Authorization: Bearer $TOKEN" \
     -H 'Content-Type: application/json' \
     -d '{
       "amount": "25.00",
       "creditor_name": "Beneficiario",
       "creditor_cpf_cnpj": "01688166360",
       "creditor_key": {"type": "CPF", "value": "01688166360"}
     }'
   ```

   `account_id` é opcional: sem ele, o callback usa a primeira conta do usuário autenticado **na detentora**.

2. Abra `login_url`; o usuário autentica na detentora.
3. A detentora redireciona para `CALLBACK_URL?code=...&state=...`.
4. `GET /callback` troca o `code` por JWT da detentora, resolve a conta, cria o consentimento ASPSP e submete o pagamento.
5. Retorna `payment_id`, `consent_id`, `status`.

> Dois tokens diferentes convivem aqui: o **JWT desta Iniciadora** (quem é o usuário no `Authorization`) e o **JWT da detentora** (obtido no callback, usado nas chamadas ao core). Um não substitui o outro.

### 2. Jornada JSR (pagamento sem redirect)

**Cadastro do dispositivo (uma vez por titular):**

1. `POST /enrollments` → retorna `enrollment_id` + `login_url`. O dispositivo nasce vinculado ao usuário do token.
2. Usuário autentica na detentora; o core redireciona para o callback.
3. `GET /callback` (kind=enrollment) confirma o titular, registra o FIDO, resolve a conta e **persiste o dispositivo** como `REGISTERED` (com `credential_id` + `account_id`).
4. Retorna `credential_id` e `account_id`.

**Pagamento JSR:**

1. `POST /payments/jsr` com os dados do crédito e o `enrollment_id` do
   dispositivo que autoriza o pagamento — é ele que define de qual conta o
   pagamento sai. O valor vem de `POST /enrollments`, do `GET /callback` do
   cadastro ou de `GET /enrollments`.

   ```bash
   curl -s -X POST http://localhost:8100/payments/jsr \
     -H "Authorization: Bearer $TOKEN" \
     -H 'Content-Type: application/json' \
     -d '{
       "amount": "25.00",
       "creditor_name": "Beneficiario",
       "creditor_cpf_cnpj": "01688166360",
       "creditor_key": {"type": "CPF", "value": "01688166360"},
       "enrollment_id": "enr-..."
     }'
   ```

2. Com o dispositivo `REGISTERED`, cria o consentimento JSR, autoriza com a credencial FIDO e inicia o PIX → retorna `payment_id`.
3. Sem dispositivo (ou com um `enrollment_id` de outro usuário), `HTTP 404`; com dispositivo fora de `REGISTERED`, `HTTP 409`. Ambos trazem `need_enrollment: true` e o `login_url` para cadastro.

## Comunicação com o core-banking

A iniciadora chama o core-banking **diretamente** em `CORE_BASE_URL`, usando os paths `/v1/...` (auth, contas, consents ASPSP) e `/open-banking/...` (rotas JSR). Os paths são idênticos em qualquer ambiente.

Nas chamadas às rotas JSR do core, a iniciadora envia o header `x-initiator-key` com o valor de `INITIATOR_CLIENT_SECRET`.

## Persistência local (SQLite)

Via `SQLModel`, a iniciadora mantém em `data/initiator.db`:

- **UserRecord** — usuários da Iniciadora (`username`, `password_hash`, `full_name`, `disabled`).
- **ConsentRecord** — consentimentos/pagamentos em andamento (`owner`, `consent_id`, `request_id`, `status`, `payment_id`, `payload`).
- **DeviceRecord** — dispositivos vinculados (`owner`, `enrollment_id`, `credential_id`, `username`, `account_id`, `status` `PENDING`/`REGISTERED`).

O dispositivo `REGISTERED` é o que habilita a jornada JSR (pagamento sem redirect).

O `owner` é o usuário autenticado que criou o registro, e é o que separa um titular do outro. A coluna é acrescentada automaticamente no start a bancos criados antes dela; **registros anteriores ficam com `owner` vazio** e, portanto, invisíveis para qualquer usuário autenticado — em ambiente de demonstração, o caminho é refazer o cadastro do dispositivo (ou apagar `data/initiator.db`).

## Testes

```bash
uv run --group dev pytest
```

O que os testes travam:

- [`test_assertion.py`](tests/test_assertion.py) — um vetor fixo que mantém a paridade da assinatura FIDO com a Detentora: o mesmo HMAC é calculado nos dois repositórios, e mudar o formato da mensagem em apenas um dos lados quebraria a jornada JSR em runtime.
- [`test_auth.py`](tests/test_auth.py) — a senha não volta do hash, o token só vale assinado pelo segredo certo e dentro da validade, e um usuário desabilitado deixa de autenticar na hora.
- [`test_jsr_device_selection.py`](tests/test_jsr_device_selection.py) — o pagamento usa o dispositivo pedido no corpo, e o dispositivo de outro titular responde como inexistente.
- [`test_errors.py`](tests/test_errors.py) — o motivo da recusa da detentora chega ao cliente, em vez de virar um 500 genérico.

## Estrutura

| Arquivo | Responsabilidade |
|---|---|
| [`app/main.py`](app/main.py) | Rotas da API e orquestração das jornadas |
| [`app/auth.py`](app/auth.py) | Login, usuários pré-cadastrados e a dependência que resolve o usuário do token |
| [`app/security.py`](app/security.py) | Hash de senha (PBKDF2) e emissão/validação do JWT |
| [`app/initiator.py`](app/initiator.py) | Chamadas ao core-banking (OAuth, contas, consentimentos, JSR) |
| [`app/assertion.py`](app/assertion.py) | Assinatura da asserção FIDO da jornada JSR |
| [`app/store.py`](app/store.py) | Persistência SQLite (usuários, consentimentos, dispositivos) |
| [`app/errors.py`](app/errors.py) | Tradução dos erros do core para a resposta da Iniciadora |
| [`app/config.py`](app/config.py) | Variáveis de ambiente |

## Considerações

- Esta é uma **demo educacional/técnica**: o FIDO é simplificado (mock) e a validação criptográfica WebAuthn não é realizada.
- O access token não tem revogação individual: uma vez emitido, vale até `JWT_EXPIRES_MINUTES`. O corte imediato disponível é desabilitar o usuário (`disabled`), que barra o token na próxima chamada.
- Não há política de senha, troca de senha nem bloqueio por tentativas: os usuários são uma lista conhecida, definida no ambiente.
- `JWT_SECRET` e `INITIATOR_USERS` são segredos: ficam no `.env` (não versionado), nunca no repositório.
- A iniciadora comunica-se diretamente com o core-banking em `CORE_BASE_URL`; eventuais camadas de gateway/roteamento ficam transparentes para a aplicação.
- O `INITIATOR_CLIENT_SECRET` deve ser tratado como segredo e nunca versionado.
