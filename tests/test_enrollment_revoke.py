"""Revogação de um dispositivo (enrollment JSR) na Iniciadora.

DELETE /enrollments/{id} remove o vínculo guardado na Iniciadora (o que impede
novos pagamentos JSR) e pede, best-effort, a revogação na detentora. Escopado
ao dono (o lojista).
"""

import pytest

import app.main as main
from app.store import UserRecord

USER = UserRecord(username="alice", password_hash="x")


def test_revoke_apaga_local_e_pede_ao_core(monkeypatch):
    seen = {}

    def fake_delete(enrollment_id, owner):
        seen["del"] = (enrollment_id, owner)
        return True

    monkeypatch.setattr(main, "delete_device_by_enrollment_id", fake_delete)
    monkeypatch.setattr(main._service, "revoke_js_enrollment", lambda _eid: True)

    out = main.revoke_enrollment("enr-1", USER)

    assert out == {"revoked": True, "core_revoked": True}
    assert seen["del"] == ("enr-1", "alice")  # escopado ao dono


def test_revoke_404_quando_nao_existe_e_nao_chama_core(monkeypatch):
    monkeypatch.setattr(main, "delete_device_by_enrollment_id", lambda *_: False)
    called = {"core": False}

    def core(_eid):
        called["core"] = True
        return True

    monkeypatch.setattr(main._service, "revoke_js_enrollment", core)

    with pytest.raises(main.HTTPException) as exc:
        main.revoke_enrollment("desconhecido", USER)

    assert exc.value.status_code == 404
    assert called["core"] is False  # não pede ao core se não achou local


def test_revoke_core_indisponivel_ainda_conclui(monkeypatch):
    """Se a detentora não revogar (best-effort), a revogação local vale."""
    monkeypatch.setattr(main, "delete_device_by_enrollment_id", lambda *_: True)
    monkeypatch.setattr(main._service, "revoke_js_enrollment", lambda _eid: False)

    out = main.revoke_enrollment("enr-1", USER)

    assert out == {"revoked": True, "core_revoked": False}
