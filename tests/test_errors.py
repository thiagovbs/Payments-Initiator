"""Repasse dos erros do core-banking.

Antes, qualquer raise_for_status() subia como excecao nao tratada e o cliente
recebia um 500 generico, perdendo o motivo que a Detentora tinha informado.
"""

import asyncio
import json

import httpx

from app.errors import upstream_status_handler, upstream_transport_handler

URL = "http://core.local/v1/me/payments"


def _status_error(status: int, body) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", URL)
    content = body if isinstance(body, (bytes, str)) else json.dumps(body)
    response = httpx.Response(status, request=request, content=content)
    return httpx.HTTPStatusError("erro", request=request, response=response)


async def _run(handler, exc):
    result = await handler(None, exc)
    return result.status_code, json.loads(result.body)


def test_passes_through_the_core_error_code():
    status, body = asyncio.run(
        _run(
            upstream_status_handler,
            _status_error(422, {"error": "INSUFFICIENT_BALANCE", "message": "Insufficient balance"}),
        )
    )

    assert status == 422
    assert body["error"] == "INSUFFICIENT_BALANCE"
    assert body["message"] == "Insufficient balance"
    assert body["core"]["status"] == 422


def test_reports_a_core_5xx_as_bad_gateway():
    # A falha nao e desta aplicacao, entao 5xx do core vira 502 e nao 500.
    status, body = asyncio.run(
        _run(upstream_status_handler, _status_error(503, {"error": "BOOM"}))
    )

    assert status == 502
    assert body["core"]["status"] == 503


def test_survives_a_non_json_body():
    status, body = asyncio.run(
        _run(upstream_status_handler, _status_error(400, "<html>Bad Request</html>"))
    )

    assert status == 400
    assert body["error"] == "CORE_REQUEST_FAILED"


def test_reports_an_unreachable_core_as_bad_gateway():
    request = httpx.Request("POST", URL)
    exc = httpx.ConnectError("connection refused", request=request)

    status, body = asyncio.run(_run(upstream_transport_handler, exc))

    assert status == 502
    assert body["error"] == "CORE_UNREACHABLE"
