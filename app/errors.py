"""Tradução dos erros do core-banking para a resposta da Iniciadora.

Sem isto, qualquer ``raise_for_status()`` do httpx sobe como exceção não tratada
e o FastAPI devolve um 500 genérico: o cliente perde o motivo real da recusa
(``INSUFFICIENT_BALANCE``, ``PIX_KEY_NOT_FOUND``, ``ENROLLMENT_ACCOUNT_MISMATCH``
e afins), que a Detentora tinha se dado ao trabalho de informar.
"""

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse


def _core_detail(response: httpx.Response) -> dict:
    """Extrai o corpo de erro do core, que segue {error, message}."""
    try:
        body = response.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


async def upstream_status_handler(
    _request: Request, exc: httpx.HTTPStatusError
) -> JSONResponse:
    """O core respondeu, mas com status de erro: repassa status e motivo."""
    detail = _core_detail(exc.response)
    status = exc.response.status_code

    return JSONResponse(
        # 4xx do core continua 4xx aqui, porque normalmente e o cliente que
        # precisa corrigir algo. 5xx vira 502: o erro nao e desta aplicacao.
        status_code=status if 400 <= status < 500 else 502,
        content={
            "error": detail.get("error", "CORE_REQUEST_FAILED"),
            "message": detail.get("message", "Falha na chamada ao core-banking"),
            "core": {"status": status, "url": str(exc.request.url)},
        },
    )


async def upstream_transport_handler(
    _request: Request, exc: httpx.RequestError
) -> JSONResponse:
    """O core nao respondeu: timeout, DNS, conexao recusada."""
    return JSONResponse(
        status_code=502,
        content={
            "error": "CORE_UNREACHABLE",
            "message": f"Nao foi possivel falar com o core-banking: {exc.__class__.__name__}",
            "core": {"url": str(exc.request.url) if exc.request else None},
        },
    )


def register_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(httpx.HTTPStatusError, upstream_status_handler)
    app.add_exception_handler(httpx.RequestError, upstream_transport_handler)
