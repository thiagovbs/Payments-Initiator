"""Paridade da assinatura FIDO com a Detentora.

O mesmo HMAC e calculado nos dois repos: aqui em app/assertion.py e no
mock-banking em src/modules/jsr/assertion.ts. Se alguem mudar o dominio, a
ordem dos campos ou o separador de um lado so, as duas pontas deixam de
concordar e a jornada JSR quebra em runtime, sem nada falhar antes.

O vetor abaixo e identico ao de tests/unit/assertion-vector.test.ts do outro
repo. Os dois precisam ser alterados juntos, ou um dos dois acusa.
"""

import pytest

from app.assertion import sign_fido_assertion

SECRET = "initiator-test-secret"
CONSENT_ID = "consent-1"
CREDENTIAL_ID = "credential-xpto"
CHALLENGE = "challenge-abc"
EXPECTED = "5f202708746b6cb9d29cabb819d6ee01088595296b55ed179d80c3802e4b4484"


def test_matches_the_signature_produced_by_the_typescript_side():
    assert sign_fido_assertion(SECRET, CONSENT_ID, CREDENTIAL_ID, CHALLENGE) == EXPECTED


@pytest.mark.parametrize(
    "consent_id, credential_id, challenge",
    [
        ("consent-2", CREDENTIAL_ID, CHALLENGE),
        (CONSENT_ID, "other-credential", CHALLENGE),
        (CONSENT_ID, CREDENTIAL_ID, "other-challenge"),
    ],
)
def test_signature_changes_with_every_field(consent_id, credential_id, challenge):
    assert sign_fido_assertion(SECRET, consent_id, credential_id, challenge) != EXPECTED


def test_signature_changes_with_the_secret():
    assert sign_fido_assertion("outro-segredo", CONSENT_ID, CREDENTIAL_ID, CHALLENGE) != EXPECTED
