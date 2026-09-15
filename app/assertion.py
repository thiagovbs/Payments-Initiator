"""Prova de posse da credencial FIDO na jornada JSR.

Espelha `src/modules/jsr/assertion.ts` da Detentora. Os dois lados precisam
montar exatamente a mesma mensagem, ou a verificação falha.

Isto **não é WebAuthn**. Não há chave privada no dispositivo: Iniciadora e
Detentora são dois backends que compartilham um segredo, e é o mesmo segredo
que já autentica a Iniciadora no header ``x-initiator-key``. A assinatura prova
conhecimento do challenge e da credencial corretos, amarrados àquele
consentimento — não a participação do dispositivo do titular.
"""

import hashlib
import hmac

DOMAIN = "jsr-fido-assertion"


def sign_fido_assertion(
    secret: str, consent_id: str, credential_id: str, challenge: str
) -> str:
    """Assina a tripla (consentimento, credencial, challenge) com HMAC-SHA256."""
    message = f"{DOMAIN}|{consent_id}|{credential_id}|{challenge}"
    return hmac.new(
        secret.encode("utf-8"), message.encode("utf-8"), hashlib.sha256
    ).hexdigest()
