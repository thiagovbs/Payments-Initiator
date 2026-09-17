"""Persistência simples (SQLite) do estado dos consentimentos da Iniciadora."""

from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from sqlalchemy import text
from sqlmodel import Field, Session, SQLModel, create_engine, select

from .config import settings

SQLITE_PREFIX = "sqlite:///"


def _ensure_sqlite_directory(url: str) -> None:
    """Cria o diretorio do arquivo SQLite, que o proprio engine nao cria."""
    if not url.startswith(SQLITE_PREFIX):
        return
    path = Path(url[len(SQLITE_PREFIX) :])
    if str(path.parent) not in (".", ""):
        path.parent.mkdir(parents=True, exist_ok=True)


_ensure_sqlite_directory(settings.database_url)

engine = create_engine(settings.database_url, connect_args={"check_same_thread": False})


class UserRecord(SQLModel, table=True):
    """Usuário pré-cadastrado da Iniciadora (login com senha -> JWT)."""

    id: Optional[int] = Field(default=None, primary_key=True)
    username: str = Field(index=True, unique=True)
    password_hash: str = ""
    full_name: str = ""
    disabled: bool = False
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ConsentRecord(SQLModel, table=True):
    """Representa um consentimento/pagamento em andamento."""

    id: Optional[int] = Field(default=None, primary_key=True)
    # Dono do registro: o usuário autenticado que iniciou o fluxo. É o que
    # impede um usuário de consultar ou pagar em cima do consentimento de outro.
    owner: str = Field(default="", index=True)
    consent_id: str = Field(index=True, unique=True)
    request_id: str = ""
    code: Optional[str] = None
    status: str = "CREATED"
    payment_id: Optional[str] = None
    payload: str = ""  # JSON com os dados do consentimento
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class DeviceRecord(SQLModel, table=True):
    """Dispositivo vinculado (JSR/ITP) à detentora via FIDO2.

    Um dispositivo registrado habilita as jornadas JSR (pagamento sem redirect).
    """

    id: Optional[int] = Field(default=None, primary_key=True)
    # Titular do dispositivo nesta Iniciadora. O pagamento JSR só enxerga os
    # dispositivos do próprio usuário: sem isso, conhecer um enrollment_id
    # alheio bastaria para debitar a conta de outra pessoa.
    owner: str = Field(default="", index=True)
    enrollment_id: str = Field(index=True, unique=True)
    credential_id: str = Field(index=True, unique=True)
    username: str = ""
    account_id: str = ""
    status: str = "PENDING"  # PENDING | REGISTERED
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


def init_db() -> None:
    SQLModel.metadata.create_all(engine)
    _add_missing_columns()


def _add_missing_columns() -> None:
    """Acrescenta as colunas novas em bancos criados antes delas.

    ``create_all`` cria tabelas que faltam, mas nunca altera as que já existem:
    num ``initiator.db`` anterior à autenticação, toda consulta quebraria com
    "no such column: owner". Registros antigos ficam com ``owner`` vazio e,
    portanto, fora do alcance de qualquer usuário autenticado.
    """
    if not settings.database_url.startswith(SQLITE_PREFIX):
        return
    with Session(engine) as session:
        for table in ("consentrecord", "devicerecord"):
            columns = {
                row[1]
                for row in session.execute(text(f"PRAGMA table_info({table})")).all()
            }
            if columns and "owner" not in columns:
                session.execute(
                    text(f"ALTER TABLE {table} ADD COLUMN owner VARCHAR DEFAULT ''")
                )
        session.commit()


def upsert_consent(record: ConsentRecord) -> ConsentRecord:
    with Session(engine) as session:
        existing = session.exec(
            select(ConsentRecord).where(ConsentRecord.request_id == record.request_id)
        ).first()
        if not existing and record.consent_id != record.request_id:
            existing = session.exec(
                select(ConsentRecord).where(
                    ConsentRecord.consent_id == record.consent_id
                )
            ).first()
        if existing:
            existing.owner = record.owner or existing.owner
            existing.consent_id = record.consent_id
            existing.request_id = record.request_id
            existing.code = record.code
            existing.status = record.status
            existing.payment_id = record.payment_id
            existing.payload = record.payload
            session.add(existing)
            session.commit()
            session.refresh(existing)
            return existing
        session.add(record)
        session.commit()
        session.refresh(record)
        return record


def get_by_consent_id(
    consent_id: str, owner: Optional[str] = None
) -> Optional[ConsentRecord]:
    """Busca por consent_id; com ``owner``, só devolve o registro daquele dono."""
    with Session(engine) as session:
        query = select(ConsentRecord).where(ConsentRecord.consent_id == consent_id)
        if owner is not None:
            query = query.where(ConsentRecord.owner == owner)
        return session.exec(query).first()


def get_by_request_id(request_id: str) -> Optional[ConsentRecord]:
    with Session(engine) as session:
        return session.exec(
            select(ConsentRecord).where(ConsentRecord.request_id == request_id)
        ).first()


def get_by_payment_id(
    payment_id: str, owner: Optional[str] = None
) -> Optional[ConsentRecord]:
    with Session(engine) as session:
        query = select(ConsentRecord).where(ConsentRecord.payment_id == payment_id)
        if owner is not None:
            query = query.where(ConsentRecord.owner == owner)
        return session.exec(query).first()


# ---------------------------------------------------------------------------
# Devices (JSR/ITP) — dispositivos vinculados via FIDO2
# ---------------------------------------------------------------------------


def upsert_device(record: DeviceRecord) -> DeviceRecord:
    """Insere ou atualiza um dispositivo (por enrollment_id ou credential_id)."""
    with Session(engine) as session:
        existing = session.exec(
            select(DeviceRecord).where(
                DeviceRecord.enrollment_id == record.enrollment_id
            )
        ).first()
        if not existing and record.credential_id:
            existing = session.exec(
                select(DeviceRecord).where(
                    DeviceRecord.credential_id == record.credential_id
                )
            ).first()
        if existing:
            existing.owner = record.owner or existing.owner
            existing.credential_id = record.credential_id
            existing.username = record.username
            existing.account_id = record.account_id
            existing.status = record.status
            session.add(existing)
            session.commit()
            session.refresh(existing)
            return existing
        session.add(record)
        session.commit()
        session.refresh(record)
        return record


def get_device_by_enrollment_id(
    enrollment_id: str, owner: Optional[str] = None
) -> Optional[DeviceRecord]:
    """Busca o dispositivo; com ``owner``, ignora os dispositivos de terceiros."""
    with Session(engine) as session:
        query = select(DeviceRecord).where(DeviceRecord.enrollment_id == enrollment_id)
        if owner is not None:
            query = query.where(DeviceRecord.owner == owner)
        return session.exec(query).first()


def list_devices(owner: str) -> list[DeviceRecord]:
    """Dispositivos vinculados do usuário, do mais recente para o mais antigo."""
    with Session(engine) as session:
        return list(
            session.exec(
                select(DeviceRecord)
                .where(DeviceRecord.owner == owner)
                .order_by(DeviceRecord.created_at.desc())
            ).all()
        )


def delete_device_by_enrollment_id(enrollment_id: str, owner: str) -> bool:
    """Remove o dispositivo do ``owner`` por ``enrollment_id``. Devolve se achou.

    Escopado ao dono para ninguém revogar o enrollment de outro titular.
    """
    with Session(engine) as session:
        device = session.exec(
            select(DeviceRecord).where(
                DeviceRecord.enrollment_id == enrollment_id,
                DeviceRecord.owner == owner,
            )
        ).first()
        if not device:
            return False
        session.delete(device)
        session.commit()
        return True


# ---------------------------------------------------------------------------
# Usuários da Iniciadora
# ---------------------------------------------------------------------------


def get_user(username: str) -> Optional[UserRecord]:
    with Session(engine) as session:
        return session.exec(
            select(UserRecord).where(UserRecord.username == username)
        ).first()


def upsert_user(record: UserRecord) -> UserRecord:
    """Cria o usuário ou atualiza a senha/os dados de um já existente."""
    with Session(engine) as session:
        existing = session.exec(
            select(UserRecord).where(UserRecord.username == record.username)
        ).first()
        if existing:
            existing.password_hash = record.password_hash
            existing.full_name = record.full_name
            existing.disabled = record.disabled
            session.add(existing)
            session.commit()
            session.refresh(existing)
            return existing
        session.add(record)
        session.commit()
        session.refresh(record)
        return record


def count_users() -> int:
    with Session(engine) as session:
        return len(session.exec(select(UserRecord)).all())
