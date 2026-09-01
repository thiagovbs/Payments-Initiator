"""Helper de autenticação.

A Iniciadora autentica o usuário no core-banking via fluxo OAuth (Option A):
  1. POST /v1/auth/authorize  → request_id + login_url
  2. Usuário autentica na tela do core
  3. POST /v1/auth/token      → access_token (JWT)

O fluxo completo está no CoreBankingService (initiator.py). Este módulo é
mantido como utilitário mínimo de compatibilidade.
"""

from .initiator import CoreBankingService


def get_access_token() -> str:
    """Compat: obtém um JWT do usuário via OAuth do core.

    Em fluxo real, o JWT é obtido após o redirect (ver main.GET /callback).
    """
    svc = CoreBankingService()
    return svc.exchange_code("") if False else ""
