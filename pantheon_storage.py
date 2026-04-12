from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional

from sqlalchemy import (
    Boolean,
    Column,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    and_,
    create_engine,
    delete,
    inspect,
    insert,
    select,
    text,
    update,
)
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError


metadata = MetaData()

users = Table(
    "users",
    metadata,
    Column("id", String(64), primary_key=True),
    Column("email", String(320), nullable=False, unique=True),
    Column("name", String(255), nullable=False, default=""),
    Column("avatar_url", Text, nullable=False, default=""),
    Column("auth_provider", String(32), nullable=False, default="local"),
    Column("password_salt", String(128), nullable=False),
    Column("password_hash", String(512), nullable=False),
    Column("created_at", String(64), nullable=False),
    Column("created_source", String(32), nullable=False, default="local"),
    Column("created_host", String(255), nullable=False, default=""),
    Column("last_login_at", String(64), nullable=False, default=""),
    Column("last_login_source", String(32), nullable=False, default=""),
)

sessions = Table(
    "sessions",
    metadata,
    Column("token", String(255), primary_key=True),
    Column("user_id", String(64), ForeignKey("users.id"), nullable=False),
    Column("created_at", String(64), nullable=False),
    Column("expires_at", String(64), nullable=False),
)
Index("sessions_user_id_idx", sessions.c.user_id)

auth_events = Table(
    "auth_events",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("user_id", String(64), nullable=False, default=""),
    Column("email", String(320), nullable=False, default=""),
    Column("event_type", String(64), nullable=False),
    Column("source", String(32), nullable=False, default="local"),
    Column("host", String(255), nullable=False, default=""),
    Column("details_json", Text, nullable=False, default="{}"),
    Column("created_at", String(64), nullable=False),
)

conversations = Table(
    "conversations",
    metadata,
    Column("id", String(255), primary_key=True),
    Column("owner_user_id", String(64), nullable=False, default=""),
    Column("owner_email", String(320), nullable=False, default=""),
    Column("question", Text, nullable=False, default=""),
    Column("title", Text, nullable=False, default=""),
    Column("status", String(32), nullable=False, default="idle"),
    Column("error", Text, nullable=False, default=""),
    Column("traceback", Text, nullable=False, default=""),
    Column("created_at", String(64), nullable=False, default=""),
    Column("updated_at", String(64), nullable=False, default=""),
    Column("started_at", String(64), nullable=False, default=""),
    Column("completed_at", String(64), nullable=False, default=""),
    Column("failed_at", String(64), nullable=False, default=""),
    Column("rounds", Integer, nullable=False, default=0),
    Column("dry_run", Boolean, nullable=False, default=False),
    Column("participants_json", Text, nullable=False, default="[]"),
    Column("summarizer_id", String(255), nullable=False, default=""),
    Column("summary_markdown", Text, nullable=False, default=""),
    Column("turns_json", Text, nullable=False, default="[]"),
    Column("turn_count", Integer, nullable=False, default=0),
    Column("has_summary", Boolean, nullable=False, default=False),
    Column("runtime_source", String(32), nullable=False, default="local"),
    Column("runtime_host", String(255), nullable=False, default=""),
)
Index("conversations_owner_user_id_idx", conversations.c.owner_user_id)
Index("conversations_updated_at_idx", conversations.c.updated_at)
Index("auth_events_user_id_idx", auth_events.c.user_id)
Index("auth_events_email_idx", auth_events.c.email)

_engine: Optional[Engine] = None


def database_url() -> str:
    raw = (
        str(os.environ.get("DATABASE_URL", "")).strip()
        or str(os.environ.get("POSTGRES_URL", "")).strip()
        or str(os.environ.get("POSTGRES_PRISMA_URL", "")).strip()
        or str(os.environ.get("POSTGRES_URL_NON_POOLING", "")).strip()
    )
    if raw.startswith("postgres://"):
        return "postgresql://" + raw[len("postgres://") :]
    return raw


def storage_enabled() -> bool:
    return bool(database_url())


def runtime_source() -> str:
    return "vercel" if os.environ.get("VERCEL") else "local"


def runtime_host() -> str:
    return (
        str(os.environ.get("VERCEL_URL", "")).strip()
        or str(os.environ.get("PANTHEON_BASE_URL", "")).strip()
        or "127.0.0.1"
    )


def engine() -> Engine:
    global _engine
    if _engine is None:
        _engine = create_engine(database_url(), future=True, pool_pre_ping=True)
    return _engine


def init_storage() -> None:
    if not storage_enabled():
        return
    db = engine()
    with db.begin() as connection:
        if connection.dialect.name == "postgresql":
            # Vercel cold starts can initialize the app concurrently. The advisory
            # lock keeps CREATE TABLE / CREATE INDEX idempotent across instances.
            connection.execute(text("SELECT pg_advisory_xact_lock(:lock_key)"), {"lock_key": 7_145_021})
        metadata.create_all(connection, checkfirst=True)
        _ensure_indexes(connection)


def _ensure_indexes(connection: Any) -> None:
    inspector = inspect(connection)
    existing_indexes: Dict[str, set[str]] = {}
    for table_name in ("sessions", "auth_events", "conversations"):
        existing_indexes[table_name] = {index["name"] for index in inspector.get_indexes(table_name)}

    statements = [
        (
            "sessions",
            "sessions_user_id_idx",
            "CREATE INDEX IF NOT EXISTS sessions_user_id_idx ON sessions (user_id)",
        ),
        (
            "auth_events",
            "auth_events_user_id_idx",
            "CREATE INDEX IF NOT EXISTS auth_events_user_id_idx ON auth_events (user_id)",
        ),
        (
            "auth_events",
            "auth_events_email_idx",
            "CREATE INDEX IF NOT EXISTS auth_events_email_idx ON auth_events (email)",
        ),
        (
            "conversations",
            "conversations_owner_user_id_idx",
            "CREATE INDEX IF NOT EXISTS conversations_owner_user_id_idx ON conversations (owner_user_id)",
        ),
        (
            "conversations",
            "conversations_updated_at_idx",
            "CREATE INDEX IF NOT EXISTS conversations_updated_at_idx ON conversations (updated_at)",
        ),
    ]
    for table_name, index_name, statement in statements:
        if index_name in existing_indexes.get(table_name, set()):
            continue
        connection.execute(text(statement))


def _json_dump(value: Any, default: str) -> str:
    if value is None:
        return default
    return json.dumps(value, ensure_ascii=True)


def _row_to_dict(row: Any) -> Dict[str, Any]:
    return dict(row._mapping)


def fetch_user_by_email(email: str) -> Optional[Dict[str, Any]]:
    with engine().begin() as connection:
        row = connection.execute(select(users).where(users.c.email == email)).mappings().first()
    return dict(row) if row else None


def fetch_user_by_id(user_id: str) -> Optional[Dict[str, Any]]:
    with engine().begin() as connection:
        row = connection.execute(select(users).where(users.c.id == user_id)).mappings().first()
    return dict(row) if row else None


def insert_user(payload: Dict[str, Any]) -> Dict[str, Any]:
    values = dict(payload)
    values.setdefault("created_source", runtime_source())
    values.setdefault("created_host", runtime_host())
    values.setdefault("last_login_at", "")
    values.setdefault("last_login_source", "")
    try:
        with engine().begin() as connection:
            connection.execute(insert(users).values(**values))
    except IntegrityError as exc:
        raise ValueError("An account with that email already exists.") from exc
    return values


def update_user(user_id: str, patch: Dict[str, Any]) -> None:
    with engine().begin() as connection:
        connection.execute(update(users).where(users.c.id == user_id).values(**patch))


def insert_session(token: str, user_id: str, created_at: str, expires_at: str) -> None:
    with engine().begin() as connection:
        connection.execute(
            insert(sessions).values(
                token=token,
                user_id=user_id,
                created_at=created_at,
                expires_at=expires_at,
            )
        )


def delete_session(token: str) -> None:
    with engine().begin() as connection:
        connection.execute(delete(sessions).where(sessions.c.token == token))


def fetch_session_user(token: str) -> Optional[Dict[str, Any]]:
    with engine().begin() as connection:
        row = (
            connection.execute(
                select(
                    users.c.id,
                    users.c.email,
                    users.c.name,
                    users.c.avatar_url,
                    users.c.auth_provider,
                    users.c.created_at,
                    users.c.created_source,
                    users.c.created_host,
                    users.c.last_login_at,
                    users.c.last_login_source,
                    sessions.c.expires_at,
                ).select_from(users.join(sessions, users.c.id == sessions.c.user_id)).where(sessions.c.token == token)
            )
            .mappings()
            .first()
        )
    return dict(row) if row else None


def record_auth_event(
    *,
    user_id: str = "",
    email: str = "",
    event_type: str,
    created_at: str,
    details: Optional[Dict[str, Any]] = None,
) -> None:
    with engine().begin() as connection:
        connection.execute(
            insert(auth_events).values(
                user_id=user_id,
                email=email,
                event_type=event_type,
                source=runtime_source(),
                host=runtime_host(),
                details_json=_json_dump(details or {}, "{}"),
                created_at=created_at,
            )
        )


def claim_ownerless_conversations(user_id: str, email: str) -> int:
    with engine().begin() as connection:
        result = connection.execute(
            update(conversations)
            .where((conversations.c.owner_user_id == "") | conversations.c.owner_user_id.is_(None))
            .values(owner_user_id=user_id, owner_email=email)
        )
    return int(result.rowcount or 0)


def upsert_conversation(payload: Dict[str, Any]) -> None:
    values = {
        "id": payload["id"],
        "owner_user_id": payload.get("owner_user_id", ""),
        "owner_email": payload.get("owner_email", ""),
        "question": payload.get("question", ""),
        "title": payload.get("title", ""),
        "status": payload.get("status", "idle"),
        "error": payload.get("error", ""),
        "traceback": payload.get("traceback", ""),
        "created_at": payload.get("created_at", ""),
        "updated_at": payload.get("updated_at", ""),
        "started_at": payload.get("started_at", ""),
        "completed_at": payload.get("completed_at", ""),
        "failed_at": payload.get("failed_at", ""),
        "rounds": int(payload.get("rounds", 0) or 0),
        "dry_run": bool(payload.get("dry_run", False)),
        "participants_json": _json_dump(payload.get("participants", []), "[]"),
        "summarizer_id": payload.get("summarizer_id", ""),
        "summary_markdown": payload.get("summary_markdown", ""),
        "turns_json": _json_dump(payload.get("turns", []), "[]"),
        "turn_count": int(payload.get("turn_count", 0) or 0),
        "has_summary": bool(payload.get("has_summary", False)),
        "runtime_source": payload.get("runtime_source", runtime_source()),
        "runtime_host": payload.get("runtime_host", runtime_host()),
    }

    with engine().begin() as connection:
        existing = connection.execute(select(conversations.c.id).where(conversations.c.id == values["id"])).first()
        if existing is None:
            connection.execute(insert(conversations).values(**values))
        else:
            connection.execute(update(conversations).where(conversations.c.id == values["id"]).values(**values))


def fetch_conversation(conversation_id: str) -> Optional[Dict[str, Any]]:
    with engine().begin() as connection:
        row = connection.execute(select(conversations).where(conversations.c.id == conversation_id)).mappings().first()
    if not row:
        return None
    return parse_conversation_record(dict(row))


def list_conversations_for_user(user_id: str) -> List[Dict[str, Any]]:
    with engine().begin() as connection:
        rows = (
            connection.execute(
                select(conversations).where(conversations.c.owner_user_id == user_id).order_by(conversations.c.updated_at.desc())
            )
            .mappings()
            .all()
        )
    return [parse_conversation_record(dict(row)) for row in rows]


def parse_conversation_record(row: Dict[str, Any]) -> Dict[str, Any]:
    payload = dict(row)
    payload["participants"] = json.loads(payload.get("participants_json") or "[]")
    payload["turns"] = json.loads(payload.get("turns_json") or "[]")
    return payload
