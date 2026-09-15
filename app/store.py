"""Persistência simples (SQLite) do estado dos consentimentos da Iniciadora."""

from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

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


class ConsentRecord(SQLModel, table=True):
    """Representa um consentimento/pagamento em andamento."""

    id: Optional[int] = Field(default=None, primary_key=True)
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
    enrollment_id: str = Field(index=True, unique=True)
    credential_id: str = Field(index=True, unique=True)
    username: str = ""
    account_id: str = ""
    status: str = "PENDING"  # PENDING | REGISTERED
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


def init_db() -> None:
    SQLModel.metadata.create_all(engine)


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


def get_by_consent_id(consent_id: str) -> Optional[ConsentRecord]:
    with Session(engine) as session:
        return session.exec(
            select(ConsentRecord).where(ConsentRecord.consent_id == consent_id)
        ).first()


def get_by_request_id(request_id: str) -> Optional[ConsentRecord]:
    with Session(engine) as session:
        return session.exec(
            select(ConsentRecord).where(ConsentRecord.request_id == request_id)
        ).first()


def get_by_payment_id(payment_id: str) -> Optional[ConsentRecord]:
    with Session(engine) as session:
        return session.exec(
            select(ConsentRecord).where(ConsentRecord.payment_id == payment_id)
        ).first()


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
    enrollment_id: str,
) -> Optional[DeviceRecord]:
    with Session(engine) as session:
        return session.exec(
            select(DeviceRecord).where(DeviceRecord.enrollment_id == enrollment_id)
        ).first()
