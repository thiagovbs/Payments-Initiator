FROM ghcr.io/astral-sh/uv:python3.11-bookworm-slim

LABEL org.opencontainers.image.title="payment-initiator"
LABEL org.opencontainers.image.description="Iniciadora de pagamento Open Finance (PISP) em Python/FastAPI"

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Instala as dependências a partir do lockfile (camada cacheável)
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

# Código da aplicação
COPY app ./app

EXPOSE 8100

CMD ["uv", "run", "--frozen", "--no-dev", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8100"]
