#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import math
import mimetypes
import os
import secrets
import sqlite3
import ssl
import threading
import traceback
from datetime import datetime
from http.cookies import SimpleCookie
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from uuid import uuid4

import orchestrator as orch
import pantheon_billing as billing
import pantheon_storage as storage

try:
    import certifi
except Exception:  # pragma: no cover - optional dependency fallback
    certifi = None


APP_ROOT = Path(__file__).resolve().parent
WEB_ROOT = APP_ROOT / "web"
IS_VERCEL = bool(os.environ.get("VERCEL"))
DATA_ROOT = Path(os.environ.get("PANTHEON_DATA_DIR", "/tmp/pantheon" if IS_VERCEL else str(APP_ROOT)))
RUNS_ROOT = DATA_ROOT / orch.DEFAULT_OUTPUT_DIR
WEB_STATE_FILENAME = "web_state.json"
AUTH_DB_PATH = DATA_ROOT / "pantheon_auth.db"
SESSION_COOKIE_NAME = "pantheon_session"
GOOGLE_STATE_COOKIE_NAME = "pantheon_google_state"
SESSION_MAX_AGE = 60 * 60 * 24 * 30
GOOGLE_STATE_MAX_AGE = 60 * 10

ACTIVE_RUNS: Dict[str, threading.Thread] = {}
ACTIVE_RUNS_LOCK = threading.Lock()


def iso_now() -> str:
    return datetime.now().isoformat()


def read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return default


def web_state_path(run_dir: Path) -> Path:
    return run_dir / WEB_STATE_FILENAME


def read_web_state(run_dir: Path) -> Dict[str, Any]:
    return read_json(web_state_path(run_dir), {})


def write_web_state(run_dir: Path, patch: Dict[str, Any]) -> Dict[str, Any]:
    current = read_web_state(run_dir)
    current.update(patch)
    current["updated_at"] = iso_now()
    run_dir.mkdir(parents=True, exist_ok=True)
    web_state_path(run_dir).write_text(json.dumps(current, indent=2) + "\n", encoding="utf-8")
    return current


def is_run_dir(path: Path) -> bool:
    return path.is_dir() and not path.name.startswith(".") and (path / "run.json").exists()


def is_active_run(run_id: str) -> bool:
    with ACTIVE_RUNS_LOCK:
        thread = ACTIVE_RUNS.get(run_id)
        if thread and thread.is_alive():
            return True
        if thread and not thread.is_alive():
            ACTIVE_RUNS.pop(run_id, None)
    return False


def register_active_run(run_id: str, thread: threading.Thread) -> None:
    with ACTIVE_RUNS_LOCK:
        ACTIVE_RUNS[run_id] = thread


def clear_active_run(run_id: str) -> None:
    with ACTIVE_RUNS_LOCK:
        ACTIVE_RUNS.pop(run_id, None)


def should_run_inline() -> bool:
    return IS_VERCEL


def durable_storage_enabled() -> bool:
    return storage.storage_enabled()


def auth_db() -> sqlite3.Connection:
    if durable_storage_enabled():
        raise RuntimeError("SQLite auth_db should not be used when DATABASE_URL is configured.")
    AUTH_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(AUTH_DB_PATH)
    connection.row_factory = sqlite3.Row
    return connection


def init_auth_db() -> None:
    if durable_storage_enabled():
        storage.init_storage()
        billing.init_billing_storage()
        return
    connection = auth_db()
    try:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
              id TEXT PRIMARY KEY,
              email TEXT NOT NULL UNIQUE,
              name TEXT NOT NULL DEFAULT '',
              avatar_url TEXT NOT NULL DEFAULT '',
              auth_provider TEXT NOT NULL DEFAULT 'local',
              password_salt TEXT NOT NULL,
              password_hash TEXT NOT NULL,
              created_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS sessions (
              token TEXT PRIMARY KEY,
              user_id TEXT NOT NULL,
              created_at TEXT NOT NULL,
              expires_at TEXT NOT NULL,
              FOREIGN KEY(user_id) REFERENCES users(id)
            )
            """
        )
        connection.execute("CREATE INDEX IF NOT EXISTS sessions_user_id_idx ON sessions(user_id)")
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(users)").fetchall()}
        if "name" not in columns:
            connection.execute("ALTER TABLE users ADD COLUMN name TEXT NOT NULL DEFAULT ''")
        if "avatar_url" not in columns:
            connection.execute("ALTER TABLE users ADD COLUMN avatar_url TEXT NOT NULL DEFAULT ''")
        if "auth_provider" not in columns:
            connection.execute("ALTER TABLE users ADD COLUMN auth_provider TEXT NOT NULL DEFAULT 'local'")
        connection.commit()
    finally:
        connection.close()


def normalize_email(value: str) -> str:
    return str(value or "").strip().lower()


def normalize_name(value: str) -> str:
    return " ".join(str(value or "").strip().split())


def hash_password(password: str, salt_hex: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), 310_000).hex()


def create_user(email: str, password: str, name: str = "", avatar_url: str = "", auth_provider: str = "local") -> Dict[str, Any]:
    normalized_email = normalize_email(email)
    normalized_name = normalize_name(name)
    if "@" not in normalized_email or "." not in normalized_email.split("@")[-1]:
        raise ValueError("Enter a valid email address.")
    if not normalized_name:
        raise ValueError("Enter your name.")
    if len(password) < 8:
        raise ValueError("Passwords must be at least 8 characters.")

    user = {
        "id": uuid4().hex,
        "email": normalized_email,
        "name": normalized_name,
        "avatar_url": str(avatar_url or "").strip(),
        "auth_provider": str(auth_provider or "local").strip() or "local",
        "password_salt": secrets.token_hex(16),
        "created_at": iso_now(),
    }
    user["password_hash"] = hash_password(password, user["password_salt"])

    if durable_storage_enabled():
        persisted = storage.insert_user(user)
        if durable_storage_enabled():
            billing.ensure_account_for_user(persisted)
        storage.record_auth_event(
            user_id=persisted["id"],
            email=persisted["email"],
            event_type="signup",
            created_at=persisted["created_at"],
            details={"auth_provider": persisted["auth_provider"], "created_source": storage.runtime_source()},
        )
        return persisted

    connection = auth_db()
    try:
        try:
            connection.execute(
                """
                INSERT INTO users (id, email, name, avatar_url, auth_provider, password_salt, password_hash, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user["id"],
                    user["email"],
                    user["name"],
                    user["avatar_url"],
                    user["auth_provider"],
                    user["password_salt"],
                    user["password_hash"],
                    user["created_at"],
                ),
            )
            connection.commit()
        except sqlite3.IntegrityError as exc:
            raise ValueError("An account with that email already exists.") from exc
    finally:
        connection.close()
    return user


def upsert_google_user(email: str, name: str, avatar_url: str = "") -> Dict[str, Any]:
    normalized_email = normalize_email(email)
    normalized_name = normalize_name(name)
    if "@" not in normalized_email or "." not in normalized_email.split("@")[-1]:
        raise ValueError("Enter a valid email address.")
    if not normalized_name:
        raise ValueError("Google did not return a valid name.")

    if durable_storage_enabled():
        row = storage.fetch_user_by_email(normalized_email)
        if row is None:
            user = {
                "id": uuid4().hex,
                "email": normalized_email,
                "name": normalized_name,
                "avatar_url": str(avatar_url or "").strip(),
                "auth_provider": "google",
                "password_salt": secrets.token_hex(16),
                "created_at": iso_now(),
            }
            user["password_hash"] = hash_password(secrets.token_urlsafe(32), user["password_salt"])
            persisted = storage.insert_user(user)
            billing.ensure_account_for_user(persisted)
            storage.record_auth_event(
                user_id=persisted["id"],
                email=persisted["email"],
                event_type="google_signup",
                created_at=persisted["created_at"],
                details={"auth_provider": "google"},
            )
            return persisted
        patch = {
            "name": normalized_name or row.get("name", ""),
            "avatar_url": str(avatar_url or "").strip() or row.get("avatar_url", ""),
        }
        storage.update_user(row["id"], patch)
        refreshed = storage.fetch_user_by_id(row["id"])
        if refreshed:
            billing.ensure_account_for_user(refreshed)
        storage.record_auth_event(
            user_id=row["id"],
            email=row["email"],
            event_type="google_login",
            created_at=iso_now(),
            details={"auth_provider": "google"},
        )
        return refreshed or {**row, **patch}

    connection = auth_db()
    try:
        row = connection.execute(
            "SELECT id, email, name, avatar_url, auth_provider, created_at FROM users WHERE email = ?",
            (normalized_email,),
        ).fetchone()
        if row is None:
            user = {
                "id": uuid4().hex,
                "email": normalized_email,
                "name": normalized_name,
                "avatar_url": str(avatar_url or "").strip(),
                "auth_provider": "google",
                "password_salt": secrets.token_hex(16),
                "created_at": iso_now(),
            }
            user["password_hash"] = hash_password(secrets.token_urlsafe(32), user["password_salt"])
            connection.execute(
                """
                INSERT INTO users (id, email, name, avatar_url, auth_provider, password_salt, password_hash, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user["id"],
                    user["email"],
                    user["name"],
                    user["avatar_url"],
                    user["auth_provider"],
                    user["password_salt"],
                    user["password_hash"],
                    user["created_at"],
                ),
            )
            connection.commit()
            return {
                "id": user["id"],
                "email": user["email"],
                "name": user["name"],
                "avatar_url": user["avatar_url"],
                "auth_provider": user["auth_provider"],
                "created_at": user["created_at"],
            }

        updated_name = normalized_name or row["name"]
        updated_avatar = str(avatar_url or "").strip() or row["avatar_url"]
        connection.execute(
            "UPDATE users SET name = ?, avatar_url = ? WHERE id = ?",
            (updated_name, updated_avatar, row["id"]),
        )
        connection.commit()
        return {
            "id": row["id"],
            "email": row["email"],
            "name": updated_name,
            "avatar_url": updated_avatar,
            "auth_provider": row["auth_provider"],
            "created_at": row["created_at"],
        }
    finally:
        connection.close()


def authenticate_user(email: str, password: str) -> Dict[str, Any]:
    normalized_email = normalize_email(email)
    if durable_storage_enabled():
        row = storage.fetch_user_by_email(normalized_email)
        if row is None:
            raise ValueError("Invalid email or password.")
        candidate = hash_password(password, row["password_salt"])
        if not hmac.compare_digest(candidate, row["password_hash"]):
            raise ValueError("Invalid email or password.")
        storage.update_user(
            row["id"],
            {"last_login_at": iso_now(), "last_login_source": storage.runtime_source()},
        )
        storage.record_auth_event(
            user_id=row["id"],
            email=row["email"],
            event_type="login",
            created_at=iso_now(),
            details={"auth_provider": row.get("auth_provider", "local")},
        )
        refreshed = storage.fetch_user_by_id(row["id"])
        return refreshed or row
    connection = auth_db()
    try:
        row = connection.execute(
            "SELECT id, email, name, avatar_url, auth_provider, password_salt, password_hash, created_at FROM users WHERE email = ?",
            (normalized_email,),
        ).fetchone()
    finally:
        connection.close()
    if row is None:
        raise ValueError("Invalid email or password.")
    candidate = hash_password(password, row["password_salt"])
    if not hmac.compare_digest(candidate, row["password_hash"]):
        raise ValueError("Invalid email or password.")
    return dict(row)


def user_by_email(email: str) -> Optional[Dict[str, Any]]:
    normalized_email = normalize_email(email)
    if durable_storage_enabled():
        return storage.fetch_user_by_email(normalized_email)
    connection = auth_db()
    try:
        row = connection.execute(
            "SELECT id, email, name, avatar_url, auth_provider, created_at FROM users WHERE email = ?",
            (normalized_email,),
        ).fetchone()
    finally:
        connection.close()
    return dict(row) if row is not None else None


def change_user_password(user_id: str, current_password: str, new_password: str) -> None:
    if len(new_password) < 8:
        raise ValueError("New passwords must be at least 8 characters.")
    if durable_storage_enabled():
        row = storage.fetch_user_by_id(user_id)
        if row is None:
            raise ValueError("User not found.")
        current_hash = hash_password(current_password, row["password_salt"])
        if not hmac.compare_digest(current_hash, row["password_hash"]):
            raise ValueError("Current password is incorrect.")
        new_salt = secrets.token_hex(16)
        new_hash = hash_password(new_password, new_salt)
        storage.update_user(user_id, {"password_salt": new_salt, "password_hash": new_hash})
        storage.record_auth_event(
            user_id=user_id,
            email=row.get("email", ""),
            event_type="password_change",
            created_at=iso_now(),
            details={},
        )
        return
    connection = auth_db()
    try:
        row = connection.execute(
            "SELECT id, password_salt, password_hash FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()
        if row is None:
            raise ValueError("User not found.")
        current_hash = hash_password(current_password, row["password_salt"])
        if not hmac.compare_digest(current_hash, row["password_hash"]):
            raise ValueError("Current password is incorrect.")
        new_salt = secrets.token_hex(16)
        new_hash = hash_password(new_password, new_salt)
        connection.execute(
            "UPDATE users SET password_salt = ?, password_hash = ? WHERE id = ?",
            (new_salt, new_hash, user_id),
        )
        connection.commit()
    finally:
        connection.close()


def create_session(user_id: str) -> str:
    token = secrets.token_urlsafe(32)
    created_at = iso_now()
    expires_at = datetime.fromtimestamp(datetime.now().timestamp() + SESSION_MAX_AGE).isoformat()
    if durable_storage_enabled():
        storage.insert_session(token, user_id, created_at, expires_at)
        return token
    connection = auth_db()
    try:
        connection.execute(
            "INSERT INTO sessions (token, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
            (token, user_id, created_at, expires_at),
        )
        connection.commit()
    finally:
        connection.close()
    return token


def delete_session(token: str) -> None:
    if not token:
        return
    if durable_storage_enabled():
        storage.delete_session(token)
        return
    connection = auth_db()
    try:
        connection.execute("DELETE FROM sessions WHERE token = ?", (token,))
        connection.commit()
    finally:
        connection.close()


def user_for_session_token(token: str) -> Optional[Dict[str, Any]]:
    if not token:
        return None
    if durable_storage_enabled():
        row = storage.fetch_session_user(token)
        if row is None:
            return None
        expires_at = str(row["expires_at"])
        if expires_at and datetime.fromisoformat(expires_at) <= datetime.now():
            storage.delete_session(token)
            return None
        return {
            "id": row["id"],
            "email": row["email"],
            "name": row.get("name", ""),
            "avatar_url": row.get("avatar_url", ""),
            "auth_provider": row.get("auth_provider", "local"),
            "created_at": row["created_at"],
            "created_source": row.get("created_source", ""),
            "created_host": row.get("created_host", ""),
            "last_login_at": row.get("last_login_at", ""),
            "last_login_source": row.get("last_login_source", ""),
        }
    connection = auth_db()
    try:
        row = connection.execute(
            """
            SELECT users.id, users.email, users.name, users.avatar_url, users.auth_provider, users.created_at, sessions.expires_at
            FROM sessions
            JOIN users ON users.id = sessions.user_id
            WHERE sessions.token = ?
            """,
            (token,),
        ).fetchone()
        if row is None:
            return None
        expires_at = str(row["expires_at"])
        if expires_at and datetime.fromisoformat(expires_at) <= datetime.now():
            connection.execute("DELETE FROM sessions WHERE token = ?", (token,))
            connection.commit()
            return None
        return {
            "id": row["id"],
            "email": row["email"],
            "name": row["name"],
            "avatar_url": row["avatar_url"],
            "auth_provider": row["auth_provider"],
            "created_at": row["created_at"],
        }
    finally:
        connection.close()


def user_payload(user: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not user:
        return None
    return {
        "id": user["id"],
        "email": user["email"],
        "name": user.get("name", ""),
        "avatarUrl": user.get("avatar_url", ""),
        "authProvider": user.get("auth_provider", "local"),
        "createdAt": user.get("created_at", ""),
        "createdSource": user.get("created_source", ""),
        "createdHost": user.get("created_host", ""),
        "lastLoginAt": user.get("last_login_at", ""),
        "lastLoginSource": user.get("last_login_source", ""),
    }


def provider_catalog(user: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    return billing.provider_catalog_for_user(user)


def participant_payload_to_api(payload: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "participantId": payload.get("participant_id", ""),
        "label": payload.get("label", ""),
        "provider": payload.get("provider", ""),
        "model": payload.get("model", ""),
        "maxOutputTokens": payload.get("max_output_tokens", 0),
        "reasoning": payload.get("reasoning", "none"),
    }


def turn_to_api(turn: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "participantId": turn.get("participant_id", ""),
        "speakerLabel": turn.get("speaker_label", ""),
        "model": turn.get("model", ""),
        "provider": turn.get("provider", ""),
        "roundNumber": turn.get("round_number", 0),
        "turnIndex": turn.get("turn_index", 0),
        "responseText": turn.get("response_text", ""),
        "citations": turn.get("citations", []),
        "usage": turn.get("usage", {}),
        "isSummary": bool(turn.get("is_summary", False)),
    }


def build_conversation_payload(run_dir: Path, include_turns: bool = True) -> Dict[str, Any]:
    metadata = read_json(run_dir / "run.json", {})
    state = read_web_state(run_dir)
    turns = metadata.get("turns", [])
    summary_turn = next((turn for turn in reversed(turns) if turn.get("is_summary")), None)
    summary_markdown = str(summary_turn.get("response_text", "")).strip() if summary_turn else ""
    if not summary_markdown and (run_dir / "summary.md").exists():
        summary_markdown = (run_dir / "summary.md").read_text(encoding="utf-8").strip()

    status = state.get("status", "idle")
    if summary_markdown and turns:
        status = "completed"
    elif status == "running" and not is_active_run(run_dir.name):
        status = "interrupted"

    participants = metadata.get("participants", [])
    summarizer_id = str(metadata.get("summarizer_id", ""))
    summarizer_label = ""
    for participant in participants:
        if participant.get("participant_id") == summarizer_id:
            summarizer_label = str(participant.get("label", ""))
            break

    question = str(metadata.get("question") or state.get("question") or run_dir.name).strip()
    title = question if len(question) <= 72 else question[:69] + "..."
    payload = {
        "id": run_dir.name,
        "title": title,
        "question": question,
        "status": status,
        "error": state.get("error", ""),
        "createdAt": state.get("created_at") or metadata.get("generated_at") or "",
        "updatedAt": state.get("updated_at") or metadata.get("generated_at") or "",
        "config": {
            "rounds": int(metadata.get("rounds", 0) or 0),
            "dryRun": bool(metadata.get("dry_run", False)),
            "participants": [participant_payload_to_api(item) for item in participants],
            "summarizerId": summarizer_id,
            "summarizerLabel": summarizer_label,
        },
        "turnCount": len([turn for turn in turns if not turn.get("is_summary")]),
        "hasSummary": bool(summary_markdown),
        "isActive": is_active_run(run_dir.name),
    }
    if include_turns:
        payload["turns"] = [turn_to_api(turn) for turn in turns if not turn.get("is_summary")]
        payload["summaryMarkdown"] = summary_markdown
    return payload


def build_conversation_payload_from_record(record: Dict[str, Any], include_turns: bool = True) -> Dict[str, Any]:
    participants = record.get("participants", []) or []
    turns = record.get("turns", []) or []
    summarizer_id = str(record.get("summarizer_id", ""))
    summarizer_label = ""
    for participant in participants:
        if participant.get("participant_id") == summarizer_id:
            summarizer_label = str(participant.get("label", ""))
            break
    payload = {
        "id": record["id"],
        "title": record.get("title", ""),
        "question": record.get("question", ""),
        "status": record.get("status", "idle"),
        "error": record.get("error", ""),
        "createdAt": record.get("created_at", ""),
        "updatedAt": record.get("updated_at", ""),
        "config": {
            "rounds": int(record.get("rounds", 0) or 0),
            "dryRun": bool(record.get("dry_run", False)),
            "participants": [participant_payload_to_api(item) for item in participants],
            "summarizerId": summarizer_id,
            "summarizerLabel": summarizer_label,
        },
        "turnCount": int(record.get("turn_count", 0) or 0),
        "hasSummary": bool(record.get("has_summary", False)),
        "isActive": False if should_run_inline() else is_active_run(record["id"]),
    }
    if include_turns:
        payload["turns"] = [turn_to_api(turn) for turn in turns if not turn.get("is_summary")]
        payload["summaryMarkdown"] = str(record.get("summary_markdown", "") or "")
    return payload


def conversation_snapshot_from_run_dir(run_dir: Path) -> Dict[str, Any]:
    metadata = read_json(run_dir / "run.json", {})
    state = read_web_state(run_dir)
    turns = metadata.get("turns", []) or []
    summary_turn = next((turn for turn in reversed(turns) if turn.get("is_summary")), None)
    summary_markdown = str(summary_turn.get("response_text", "")).strip() if summary_turn else ""
    if not summary_markdown and (run_dir / "summary.md").exists():
        summary_markdown = (run_dir / "summary.md").read_text(encoding="utf-8").strip()
    question = str(metadata.get("question") or state.get("question") or run_dir.name).strip()
    title = question if len(question) <= 72 else question[:69] + "..."
    status = state.get("status", "idle")
    if summary_markdown and turns:
        status = "completed"
    elif status == "running" and not should_run_inline() and not is_active_run(run_dir.name):
        status = "interrupted"
    return {
        "id": run_dir.name,
        "owner_user_id": str(state.get("owner_user_id", "")).strip(),
        "owner_email": str(state.get("owner_email", "")).strip(),
        "question": question,
        "title": title,
        "status": status,
        "error": str(state.get("error", "") or ""),
        "traceback": str(state.get("traceback", "") or ""),
        "created_at": str(state.get("created_at") or metadata.get("generated_at") or ""),
        "updated_at": str(state.get("updated_at") or metadata.get("generated_at") or ""),
        "started_at": str(state.get("started_at", "") or ""),
        "completed_at": str(state.get("completed_at", "") or ""),
        "failed_at": str(state.get("failed_at", "") or ""),
        "rounds": int(metadata.get("rounds", 0) or 0),
        "dry_run": bool(metadata.get("dry_run", False)),
        "participants": metadata.get("participants", []) or [],
        "summarizer_id": str(metadata.get("summarizer_id", "") or ""),
        "summary_markdown": summary_markdown,
        "turns": turns,
        "turn_count": len([turn for turn in turns if not turn.get("is_summary")]),
        "has_summary": bool(summary_markdown),
        "runtime_source": storage.runtime_source(),
        "runtime_host": storage.runtime_host(),
    }


def sync_run_dir_to_storage(run_dir: Path) -> None:
    if not durable_storage_enabled():
        return
    storage.upsert_conversation(conversation_snapshot_from_run_dir(run_dir))


def backfill_runs_to_storage() -> None:
    if not durable_storage_enabled():
        return
    if not RUNS_ROOT.exists():
        return
    for path in RUNS_ROOT.iterdir():
        if is_run_dir(path):
            sync_run_dir_to_storage(path)


def resume_state_from_storage(run_id: str) -> orch.ResumeState:
    record = storage.fetch_conversation(run_id)
    if not record:
        raise FileNotFoundError(f"No conversation found for {run_id}")
    participants = orch.participants_from_payload(record.get("participants", []))
    turns = [orch.turn_from_dict(item) for item in record.get("turns", [])]
    summarizer_id = str(record.get("summarizer_id", "") or (participants[-1].participant_id if participants else ""))
    metadata = {
        "question": record.get("question", ""),
        "rounds": int(record.get("rounds", 0) or 0),
        "participants": record.get("participants", []),
        "summarizer_id": summarizer_id,
        "turns": record.get("turns", []),
        "dry_run": bool(record.get("dry_run", False)),
    }
    return orch.ResumeState(
        run_dir=RUNS_ROOT / run_id,
        metadata=metadata,
        question=str(record.get("question", "")),
        rounds=int(record.get("rounds", 0) or 0),
        participants=participants,
        summarizer_id=summarizer_id,
        turns=turns,
        summary_markdown=str(record.get("summary_markdown", "") or ""),
    )


def run_owner_id(run_dir: Path) -> str:
    if durable_storage_enabled():
        record = storage.fetch_conversation(run_dir.name)
        return str(record.get("owner_user_id", "")).strip() if record else ""
    state = read_web_state(run_dir)
    return str(state.get("owner_user_id", "")).strip()


def user_can_access_run(run_dir: Path, user: Optional[Dict[str, Any]]) -> bool:
    if not user:
        return False
    if durable_storage_enabled():
        record = storage.fetch_conversation(run_dir.name)
        if not record:
            return False
        return user.get("id") == str(record.get("owner_user_id", "")).strip()
    owner_id = run_owner_id(run_dir)
    if not owner_id:
        return False
    return user.get("id") == owner_id


def assign_ownerless_runs_to_user(user: Dict[str, Any]) -> int:
    if durable_storage_enabled():
        return storage.claim_ownerless_conversations(user["id"], user["email"])
    assigned = 0
    RUNS_ROOT.mkdir(parents=True, exist_ok=True)
    for path in RUNS_ROOT.iterdir():
        if not is_run_dir(path):
            continue
        state = read_web_state(path)
        owner_id = str(state.get("owner_user_id", "")).strip()
        if owner_id:
            continue
        write_web_state(
            path,
            {
                "owner_user_id": user["id"],
                "owner_email": user["email"],
            },
        )
        assigned += 1
    return assigned


def user_run_stats(user: Dict[str, Any]) -> Dict[str, Any]:
    conversations = list_conversations(user)
    total_runs = len(conversations)
    completed_runs = len([item for item in conversations if item.get("status") == "completed"])
    dry_runs = len([item for item in conversations if item.get("config", {}).get("dryRun")])
    total_turns = sum(int(item.get("turnCount", 0) or 0) for item in conversations)
    latest_run = conversations[0].get("updatedAt", "") if conversations else ""
    return {
        "totalRuns": total_runs,
        "completedRuns": completed_runs,
        "dryRuns": dry_runs,
        "totalTurns": total_turns,
        "latestRunAt": latest_run,
    }


def billing_overview_for_user(user: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    return billing.billing_snapshot_for_user(user)


def account_overview_for_user(user: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not user:
        return None
    return {
        "user": user_payload(user),
        "stats": user_run_stats(user),
        "billing": billing_overview_for_user(user),
    }


def billing_quote_from_payload(
    payload: Dict[str, Any],
    user: Optional[Dict[str, Any]],
    *,
    completed_turns: Optional[List[orch.ConversationTurn]] = None,
) -> Dict[str, Any]:
    parsed = parse_request_payload(payload, allow_env_fallback=True)
    return billing.quote_for_user(
        user,
        parsed["question"],
        parsed["rounds"],
        parsed["participants"],
        parsed["summarizer_id"],
        parsed["dry_run"],
        completed_turns=completed_turns,
    )


def google_auth_enabled() -> bool:
    return bool(os.environ.get("GOOGLE_CLIENT_ID") and os.environ.get("GOOGLE_CLIENT_SECRET"))


def verified_ssl_context() -> ssl.SSLContext:
    if certifi is not None:
        return ssl.create_default_context(cafile=certifi.where())
    return ssl.create_default_context()


def app_base_url(handler: "AppHandler") -> str:
    configured = str(os.environ.get("PANTHEON_BASE_URL", "")).strip().rstrip("/")
    if configured:
        return configured
    vercel_url = str(os.environ.get("VERCEL_URL", "")).strip()
    if vercel_url:
        return f"https://{vercel_url.strip().rstrip('/')}"
    forwarded_proto = str(handler.headers.get("X-Forwarded-Proto", "")).strip()
    forwarded_host = str(handler.headers.get("X-Forwarded-Host", "")).strip()
    scheme = forwarded_proto or "http"
    host = forwarded_host or handler.headers.get("Host", "127.0.0.1:8002")
    return f"{scheme}://{host}"


def google_redirect_uri(handler: "AppHandler") -> str:
    return f"{app_base_url(handler)}/auth/google/callback"


def google_authorize_url(handler: "AppHandler", state_token: str) -> str:
    query = urlencode(
        {
            "client_id": os.environ.get("GOOGLE_CLIENT_ID", ""),
            "redirect_uri": google_redirect_uri(handler),
            "response_type": "code",
            "scope": "openid email profile",
            "access_type": "online",
            "prompt": "select_account",
            "state": state_token,
        }
    )
    return f"https://accounts.google.com/o/oauth2/v2/auth?{query}"


def exchange_google_code(handler: "AppHandler", code: str) -> Dict[str, Any]:
    payload = urlencode(
        {
            "code": code,
            "client_id": os.environ.get("GOOGLE_CLIENT_ID", ""),
            "client_secret": os.environ.get("GOOGLE_CLIENT_SECRET", ""),
            "redirect_uri": google_redirect_uri(handler),
            "grant_type": "authorization_code",
        }
    ).encode("utf-8")
    request = Request(
        "https://oauth2.googleapis.com/token",
        data=payload,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=20, context=verified_ssl_context()) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Google token exchange failed: HTTP {exc.code} {body}") from exc
    except Exception as exc:
        raise RuntimeError(f"Google token exchange failed: {exc}") from exc


def fetch_google_profile(access_token: str) -> Dict[str, Any]:
    request = Request(
        "https://openidconnect.googleapis.com/v1/userinfo",
        headers={"Authorization": f"Bearer {access_token}"},
    )
    try:
        with urlopen(request, timeout=20, context=verified_ssl_context()) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Google profile request failed: HTTP {exc.code} {body}") from exc
    except Exception as exc:
        raise RuntimeError(f"Google profile request failed: {exc}") from exc


def list_conversations(user: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    if durable_storage_enabled():
        if not user:
            return []
        return [build_conversation_payload_from_record(item, include_turns=False) for item in storage.list_conversations_for_user(user["id"])]
    RUNS_ROOT.mkdir(parents=True, exist_ok=True)
    conversations = [
        build_conversation_payload(path, include_turns=False)
        for path in RUNS_ROOT.iterdir()
        if is_run_dir(path) and user_can_access_run(path, user)
    ]
    conversations.sort(key=lambda item: item.get("updatedAt", ""), reverse=True)
    return conversations


def parse_request_payload(payload: Dict[str, Any], allow_env_fallback: bool = False) -> Dict[str, Any]:
    question = str(payload.get("question", "")).strip()
    participants_raw = payload.get("participants") or []
    participants = orch.participants_from_payload(participants_raw)
    rounds = int(payload.get("rounds", orch.DEFAULT_ROUNDS))
    dry_run = bool(payload.get("dry_run", payload.get("dryRun", False)))
    if rounds < 1:
        raise ValueError("Rounds must be at least 1.")
    summarizer_id = str(payload.get("summarizerId") or participants[-1].participant_id).strip()
    orch.participant_by_id(participants, summarizer_id)
    runtime_keys = {} if dry_run else orch.extract_runtime_keys(participants, participants_raw, use_env_fallback=True)
    return {
        "question": question,
        "rounds": rounds,
        "participants": participants,
        "participant_payloads": participants_raw,
        "runtime_keys": runtime_keys,
        "summarizer_id": summarizer_id,
        "dry_run": dry_run,
    }


def persist_progress(
    run_dir: Path,
    question: str,
    rounds: int,
    participants: List[orch.ParticipantConfig],
    summarizer_id: str,
    turns: List[orch.ConversationTurn],
    summary_markdown: str,
    dry_run: bool,
) -> None:
    orch.write_markdown_logs(run_dir, question, participants, summarizer_id, turns, summary_markdown)
    orch.write_run_metadata(run_dir, question, rounds, participants, summarizer_id, turns, dry_run)
    write_web_state(
        run_dir,
        {
            "question": question,
            "status": "running",
            "error": "",
            "last_turns": len(turns),
        },
    )
    sync_run_dir_to_storage(run_dir)


def execute_conversation(
    run_dir: Path,
    question: str,
    rounds: int,
    participants: List[orch.ParticipantConfig],
    summarizer_id: str,
    runtime_keys: Dict[str, str],
    dry_run: bool,
    resume_state: Optional[orch.ResumeState] = None,
    usage_event_id: str = "",
    billing_turns_start_index: int = 0,
) -> None:
    run_id = run_dir.name
    turns = list(resume_state.turns) if resume_state else []
    summary_markdown = resume_state.summary_markdown if resume_state else ""
    summarizer = orch.participant_by_id(participants, summarizer_id)
    if usage_event_id:
        billing.mark_usage_running(usage_event_id)

    write_web_state(
        run_dir,
        {
            "question": question,
            "status": "running",
            "error": "",
            "created_at": read_web_state(run_dir).get("created_at", iso_now()),
            "started_at": iso_now(),
        },
    )
    sync_run_dir_to_storage(run_dir)

    try:
        while True:
            step, round_number, participant = orch.determine_next_step(turns, participants, rounds)
            if step == "done":
                write_web_state(
                    run_dir,
                    {
                        "status": "completed",
                        "completed_at": iso_now(),
                        "error": "",
                    },
                )
                orch.write_markdown_logs(run_dir, question, participants, summarizer_id, turns, summary_markdown)
                orch.write_run_metadata(run_dir, question, rounds, participants, summarizer_id, turns, dry_run)
                sync_run_dir_to_storage(run_dir)
                return

            if step == "participant" and participant is not None:
                prompt = orch.compose_turn_prompt(participant, question, round_number, participants, summarizer, turns)
                if dry_run:
                    turn = orch.build_stub_turn(
                        participant=participant,
                        round_number=round_number,
                        turn_index=len([item for item in turns if not item.is_summary]) + 1,
                        prompt=prompt,
                    )
                else:
                    raw_response = orch.call_provider(participant, runtime_keys[participant.participant_id], prompt)
                    turn = orch.ConversationTurn(
                        participant_id=participant.participant_id,
                        speaker_label=participant.label,
                        provider=participant.provider,
                        model=raw_response.get("model", participant.model),
                        round_number=round_number,
                        turn_index=len([item for item in turns if not item.is_summary]) + 1,
                        prompt=prompt,
                        response_text=orch.normalize_response_text(orch.extract_response_text(participant, raw_response)),
                        citations=orch.extract_response_citations(participant, raw_response),
                        usage=orch.extract_usage_metrics(participant, raw_response),
                        raw_response=raw_response,
                    )
                turns.append(turn)
                persist_progress(run_dir, question, rounds, participants, summarizer_id, turns, summary_markdown, dry_run)
                continue

            summary_prompt = orch.compose_summary_prompt(question, participants, summarizer, turns)
            if dry_run:
                summary_turn = orch.build_stub_turn(
                    participant=summarizer,
                    round_number=rounds + 1,
                    turn_index=len(turns) + 1,
                    prompt=summary_prompt,
                    is_summary=True,
                )
            else:
                raw_response = orch.call_provider(summarizer, runtime_keys[summarizer.participant_id], summary_prompt)
                summary_turn = orch.ConversationTurn(
                    participant_id=summarizer.participant_id,
                    speaker_label=f"{summarizer.label} Summary",
                    provider=summarizer.provider,
                    model=raw_response.get("model", summarizer.model),
                    round_number=rounds + 1,
                    turn_index=len(turns) + 1,
                    prompt=summary_prompt,
                    response_text=orch.normalize_response_text(orch.extract_response_text(summarizer, raw_response)),
                    citations=orch.extract_response_citations(summarizer, raw_response),
                    usage=orch.extract_usage_metrics(summarizer, raw_response),
                    raw_response=raw_response,
                    is_summary=True,
                )
            turns.append(summary_turn)
            summary_markdown = summary_turn.response_text
            orch.write_markdown_logs(run_dir, question, participants, summarizer_id, turns, summary_markdown)
            orch.write_run_metadata(run_dir, question, rounds, participants, summarizer_id, turns, dry_run)
            write_web_state(
                run_dir,
                {
                    "status": "completed",
                    "completed_at": iso_now(),
                    "error": "",
                },
            )
            sync_run_dir_to_storage(run_dir)
            return
    except orch.ApiError as exc:
        orch.write_markdown_logs(run_dir, question, participants, summarizer_id, turns, summary_markdown)
        orch.write_run_metadata(run_dir, question, rounds, participants, summarizer_id, turns, dry_run)
        write_web_state(
            run_dir,
            {
                "status": "failed",
                "error": f"{exc.provider}: {exc}",
                "failed_at": iso_now(),
            },
        )
        sync_run_dir_to_storage(run_dir)
    except Exception as exc:  # pragma: no cover
        orch.write_markdown_logs(run_dir, question, participants, summarizer_id, turns, summary_markdown)
        orch.write_run_metadata(run_dir, question, rounds, participants, summarizer_id, turns, dry_run)
        write_web_state(
            run_dir,
            {
                "status": "failed",
                "error": f"{type(exc).__name__}: {exc}",
                "traceback": traceback.format_exc(),
                "failed_at": iso_now(),
            },
        )
        sync_run_dir_to_storage(run_dir)
    finally:
        if usage_event_id:
            new_turns = turns[billing_turns_start_index:]
            status = "completed" if summary_markdown.strip() else "failed"
            try:
                billing.settle_usage_for_run(usage_event_id, new_turns, final_status=status)
            except Exception:  # pragma: no cover
                pass
        clear_active_run(run_id)


def start_new_conversation(payload: Dict[str, Any], user: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not user:
        raise PermissionError("You must be logged in to start a paid conversation.")
    parsed = parse_request_payload(payload, allow_env_fallback=False)
    question = parsed["question"]
    if not question:
        raise ValueError("Question is required.")

    RUNS_ROOT.mkdir(parents=True, exist_ok=True)
    run_id = f"{orch.now_stamp()}-{orch.slugify(question)}"
    reservation = billing.reserve_credits_for_run(
        user or {},
        conversation_id=run_id,
        question=question,
        rounds=parsed["rounds"],
        participants=parsed["participants"],
        summarizer_id=parsed["summarizer_id"],
        dry_run=parsed["dry_run"],
    )
    run_dir = RUNS_ROOT / run_id
    try:
        run_dir.mkdir(parents=True, exist_ok=True)
        write_web_state(
            run_dir,
            {
                "question": question,
                "status": "queued",
                "created_at": iso_now(),
                "updated_at": iso_now(),
                "error": "",
                "owner_user_id": user.get("id", "") if user else "",
                "owner_email": user.get("email", "") if user else "",
                "usage_event_id": reservation["usageEventId"],
                "reserved_credits": reservation["reservedCredits"],
            },
        )
        orch.write_markdown_logs(
            run_dir,
            question,
            parsed["participants"],
            parsed["summarizer_id"],
            [],
            "",
        )
        orch.write_run_metadata(
            run_dir,
            question,
            parsed["rounds"],
            parsed["participants"],
            parsed["summarizer_id"],
            [],
            parsed["dry_run"],
        )
        sync_run_dir_to_storage(run_dir)
    except Exception:
        billing.settle_usage_for_run(reservation["usageEventId"], [], final_status="failed")
        raise

    if should_run_inline():
        execute_conversation(
            run_dir,
            question,
            parsed["rounds"],
            parsed["participants"],
            parsed["summarizer_id"],
            parsed["runtime_keys"],
            parsed["dry_run"],
            usage_event_id=reservation["usageEventId"],
            billing_turns_start_index=0,
        )
        conversation = build_conversation_payload(run_dir)
        conversation["billing"] = {"reservation": reservation}
        return conversation

    thread = threading.Thread(
        target=execute_conversation,
        args=(
            run_dir,
            question,
            parsed["rounds"],
            parsed["participants"],
            parsed["summarizer_id"],
            parsed["runtime_keys"],
            parsed["dry_run"],
            None,
            reservation["usageEventId"],
            0,
        ),
        daemon=True,
    )
    register_active_run(run_dir.name, thread)
    thread.start()
    sync_run_dir_to_storage(run_dir)
    conversation = build_conversation_payload(run_dir)
    conversation["billing"] = {"reservation": reservation}
    return conversation


def resume_conversation(run_id: str, payload: Dict[str, Any], user: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not user:
        raise PermissionError("You must be logged in to resume a paid conversation.")
    run_dir = RUNS_ROOT / run_id
    if durable_storage_enabled():
        record = storage.fetch_conversation(run_id)
        if not record:
            raise FileNotFoundError(f"No conversation found for {run_id}")
        if (not user) or (user.get("id") != str(record.get("owner_user_id", "")).strip()):
            raise PermissionError("You do not have access to that conversation.")
    elif not is_run_dir(run_dir):
        raise FileNotFoundError(f"No conversation found for {run_id}")
    if not durable_storage_enabled() and not user_can_access_run(run_dir, user):
        raise PermissionError("You do not have access to that conversation.")
    if is_active_run(run_id):
        if durable_storage_enabled():
            record = storage.fetch_conversation(run_id)
            return build_conversation_payload_from_record(record) if record else build_conversation_payload(run_dir)
        return build_conversation_payload(run_dir)

    resume_state = resume_state_from_storage(run_id) if durable_storage_enabled() else orch.load_resume_state(str(run_dir))
    participant_payloads = payload.get("participants") or [orch.participant_to_metadata(item) for item in resume_state.participants]
    runtime_keys = (
        {}
        if bool(resume_state.metadata.get("dry_run", False))
        else orch.extract_runtime_keys(resume_state.participants, participant_payloads, use_env_fallback=False)
    )
    reservation = billing.reserve_credits_for_run(
        user or {},
        conversation_id=run_id,
        question=resume_state.question,
        rounds=resume_state.rounds,
        participants=resume_state.participants,
        summarizer_id=resume_state.summarizer_id,
        dry_run=bool(resume_state.metadata.get("dry_run", False)),
        completed_turns=resume_state.turns,
    )
    write_web_state(run_dir, {"usage_event_id": reservation["usageEventId"], "reserved_credits": reservation["reservedCredits"]})
    sync_run_dir_to_storage(run_dir)

    if should_run_inline():
        write_web_state(run_dir, {"status": "running", "error": ""})
        sync_run_dir_to_storage(run_dir)
        execute_conversation(
            run_dir,
            resume_state.question,
            resume_state.rounds,
            resume_state.participants,
            resume_state.summarizer_id,
            runtime_keys,
            bool(resume_state.metadata.get("dry_run", False)),
            resume_state,
            usage_event_id=reservation["usageEventId"],
            billing_turns_start_index=len(resume_state.turns),
        )
        if durable_storage_enabled():
            record = storage.fetch_conversation(run_id)
            conversation = build_conversation_payload_from_record(record) if record else build_conversation_payload(run_dir)
            conversation["billing"] = {"reservation": reservation}
            return conversation
        conversation = build_conversation_payload(run_dir)
        conversation["billing"] = {"reservation": reservation}
        return conversation

    thread = threading.Thread(
        target=execute_conversation,
        args=(
            run_dir,
            resume_state.question,
            resume_state.rounds,
            resume_state.participants,
            resume_state.summarizer_id,
            runtime_keys,
            bool(resume_state.metadata.get("dry_run", False)),
            resume_state,
            reservation["usageEventId"],
            len(resume_state.turns),
        ),
        daemon=True,
    )
    register_active_run(run_id, thread)
    thread.start()
    write_web_state(run_dir, {"status": "running", "error": ""})
    sync_run_dir_to_storage(run_dir)
    if durable_storage_enabled():
        record = storage.fetch_conversation(run_id)
        conversation = build_conversation_payload_from_record(record) if record else build_conversation_payload(run_dir)
        conversation["billing"] = {"reservation": reservation}
        return conversation
    conversation = build_conversation_payload(run_dir)
    conversation["billing"] = {"reservation": reservation}
    return conversation


def file_response_path(path: str) -> Path:
    if path == "/favicon.ico":
        relative = "favicon.svg"
    else:
        relative = "index.html" if path in {"", "/"} else path.lstrip("/")
    candidate = (WEB_ROOT / relative).resolve()
    if WEB_ROOT.resolve() not in candidate.parents and candidate != WEB_ROOT.resolve():
        raise FileNotFoundError("Invalid path")
    if not candidate.exists() or not candidate.is_file():
        raise FileNotFoundError(relative)
    return candidate


def bootstrap_script(assignments: Dict[str, Any]) -> str:
    statements = [f"window.{key} = {json.dumps(value)};" for key, value in assignments.items()]
    return f"<script>{''.join(statements)}</script>"


def render_bootstrapped_html(template_name: str, assignments: Dict[str, Any]) -> bytes:
    template = (WEB_ROOT / template_name).read_text(encoding="utf-8")
    return template.replace("__PANTHEON_BOOTSTRAP__", bootstrap_script(assignments)).encode("utf-8")


def render_home_html(user: Optional[Dict[str, Any]]) -> bytes:
    return render_bootstrapped_html(
        "index.html",
        {
            "__PANTHEON_INITIAL_USER__": user_payload(user),
            "__PANTHEON_INITIAL_BILLING__": billing_overview_for_user(user),
            "__PANTHEON_INITIAL_PROVIDERS__": provider_catalog(user),
            "__PANTHEON_INITIAL_CONVERSATIONS__": list_conversations(user),
            "__PANTHEON_INITIAL_GOOGLE_AUTH_ENABLED__": google_auth_enabled(),
        },
    )


def render_account_html(user: Optional[Dict[str, Any]]) -> bytes:
    overview = account_overview_for_user(user)
    return render_bootstrapped_html(
        "account.html",
        {
            "__PANTHEON_INITIAL_USER__": user_payload(user),
            "__PANTHEON_INITIAL_BILLING__": overview.get("billing") if overview else billing_overview_for_user(user),
            "__PANTHEON_INITIAL_ACCOUNT__": overview,
            "__PANTHEON_INITIAL_GOOGLE_AUTH_ENABLED__": google_auth_enabled(),
        },
    )


def render_pricing_html(user: Optional[Dict[str, Any]]) -> bytes:
    return render_bootstrapped_html(
        "pricing.html",
        {
            "__PANTHEON_INITIAL_USER__": user_payload(user),
            "__PANTHEON_INITIAL_BILLING__": billing_overview_for_user(user),
        },
    )


class AppHandler(BaseHTTPRequestHandler):
    server_version = "Pantheon/1.0"

    def cookies(self) -> SimpleCookie:
        cookie = SimpleCookie()
        cookie.load(self.headers.get("Cookie", ""))
        return cookie

    def current_user(self) -> Optional[Dict[str, Any]]:
        cookie = self.cookies()
        token = cookie.get(SESSION_COOKIE_NAME)
        return user_for_session_token(token.value if token else "")

    def cookie_security_suffix(self) -> str:
        base = str(os.environ.get("PANTHEON_BASE_URL", "")).strip().lower()
        if IS_VERCEL or base.startswith("https://"):
            return "; Secure"
        return ""

    def session_cookie_header(self, token: str, expires: bool = False) -> str:
        if expires:
            return f"{SESSION_COOKIE_NAME}=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0{self.cookie_security_suffix()}"
        return f"{SESSION_COOKIE_NAME}={token}; Path=/; HttpOnly; SameSite=Lax; Max-Age={SESSION_MAX_AGE}{self.cookie_security_suffix()}"

    def google_state_cookie_header(self, token: str, expires: bool = False) -> str:
        if expires:
            return f"{GOOGLE_STATE_COOKIE_NAME}=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0{self.cookie_security_suffix()}"
        return f"{GOOGLE_STATE_COOKIE_NAME}={token}; Path=/; HttpOnly; SameSite=Lax; Max-Age={GOOGLE_STATE_MAX_AGE}{self.cookie_security_suffix()}"

    def redirect(self, location: str, headers: Optional[Dict[str, Any]] = None) -> None:
        self.send_response(HTTPStatus.FOUND)
        self.send_header("Location", location)
        for key, value in (headers or {}).items():
            if isinstance(value, (list, tuple)):
                for item in value:
                    self.send_header(key, str(item))
            else:
                self.send_header(key, str(value))
        self.end_headers()

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self.send_html(render_home_html(self.current_user()))
            return

        if parsed.path == "/login":
            self.serve_static_file("login.html")
            return

        if parsed.path == "/signup":
            self.serve_static_file("signup.html")
            return

        if parsed.path == "/account":
            self.send_html(render_account_html(self.current_user()))
            return

        if parsed.path == "/privacy":
            self.serve_static_file("privacy.html")
            return

        if parsed.path == "/company":
            self.serve_static_file("company.html")
            return

        if parsed.path == "/contact":
            self.serve_static_file("contact.html")
            return

        if parsed.path == "/pricing":
            self.send_html(render_pricing_html(self.current_user()))
            return

        if parsed.path == "/auth/google/start":
            if not google_auth_enabled():
                self.redirect("/signup?error=Google+sign-in+is+not+configured+yet.")
                return
            state_token = secrets.token_urlsafe(24)
            self.redirect(
                google_authorize_url(self, state_token),
                headers={"Set-Cookie": self.google_state_cookie_header(state_token)},
            )
            return

        if parsed.path == "/auth/google/callback":
            params = parse_qs(parsed.query)
            if params.get("error"):
                self.redirect("/signup?error=Google+sign-in+was+cancelled.")
                return
            code = str((params.get("code") or [""])[0]).strip()
            returned_state = str((params.get("state") or [""])[0]).strip()
            stored_state = self.cookies().get(GOOGLE_STATE_COOKIE_NAME)
            if not code or not returned_state or not stored_state or stored_state.value != returned_state:
                self.redirect(
                    "/signup?error=Google+sign-in+could+not+be+verified.",
                    headers={"Set-Cookie": self.google_state_cookie_header("", expires=True)},
                )
                return
            try:
                token_payload = exchange_google_code(self, code)
                access_token = str(token_payload.get("access_token", "")).strip()
                if not access_token:
                    raise ValueError("Google did not return an access token.")
                profile = fetch_google_profile(access_token)
                user = upsert_google_user(
                    str(profile.get("email", "")),
                    str(profile.get("name", "")),
                    str(profile.get("picture", "")),
                )
                assign_ownerless_runs_to_user(user)
                token = create_session(user["id"])
            except Exception as exc:
                message = str(exc).strip() or "Google sign-in failed."
                self.redirect(
                    f"/signup?error={urlencode({'message': message})[8:]}",
                    headers={"Set-Cookie": self.google_state_cookie_header("", expires=True)},
                )
                return
            self.redirect(
                "/account?success=You%27ve+successfully+logged+in.",
                headers={
                    "Set-Cookie": [
                        self.session_cookie_header(token),
                        self.google_state_cookie_header("", expires=True),
                    ],
                },
            )
            return

        if parsed.path.startswith("/conversations/"):
            parts = [part for part in parsed.path.split("/") if part]
            if len(parts) == 2:
                self.serve_static_file("conversation.html")
                return

        if parsed.path == "/api/models":
            self.send_json(
                {
                    "providers": provider_catalog(self.current_user()),
                    "maxParticipants": orch.MAX_PARTICIPANTS,
                    "billing": billing_overview_for_user(self.current_user()),
                }
            )
            return

        if parsed.path == "/api/health":
            self.send_json(
                {
                    "ok": True,
                    "storage": "database" if durable_storage_enabled() else "local",
                    "runtime": "vercel" if IS_VERCEL else "local",
                    "baseUrl": app_base_url(self),
                    "googleAuthEnabled": google_auth_enabled(),
                    "billingReady": billing.billing_backend_ready(),
                    "stripeReady": billing.stripe_ready(),
                }
            )
            return

        if parsed.path == "/api/auth/me":
            self.send_json({"user": user_payload(self.current_user()), "googleAuthEnabled": google_auth_enabled()})
            return

        if parsed.path == "/api/account":
            user = self.current_user()
            if not user:
                self.send_json({"error": "You must be logged in."}, status=HTTPStatus.UNAUTHORIZED)
                return
            self.send_json({"user": user_payload(user), "stats": user_run_stats(user), "billing": billing_overview_for_user(user)})
            return

        if parsed.path == "/api/billing":
            self.send_json(billing_overview_for_user(self.current_user()))
            return

        if parsed.path == "/api/conversations":
            self.send_json({"conversations": list_conversations(self.current_user())})
            return

        if parsed.path.startswith("/api/conversations/"):
            parts = [part for part in parsed.path.split("/") if part]
            if len(parts) == 3:
                run_id = parts[2]
                if durable_storage_enabled():
                    record = storage.fetch_conversation(run_id)
                    user = self.current_user()
                    if not record or not user or user.get("id") != str(record.get("owner_user_id", "")).strip():
                        self.send_json({"error": "Conversation not found."}, status=HTTPStatus.NOT_FOUND)
                        return
                    self.send_json(build_conversation_payload_from_record(record))
                    return
                run_dir = RUNS_ROOT / run_id
                if not is_run_dir(run_dir):
                    self.send_json({"error": "Conversation not found."}, status=HTTPStatus.NOT_FOUND)
                    return
                if not user_can_access_run(run_dir, self.current_user()):
                    self.send_json({"error": "Conversation not found."}, status=HTTPStatus.NOT_FOUND)
                    return
                self.send_json(build_conversation_payload(run_dir))
                return

        try:
            self.send_static_file(file_response_path(parsed.path))
        except FileNotFoundError:
            self.send_json({"error": "Not found."}, status=HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/auth/signup":
            try:
                payload = self.read_json_body()
                if str(payload.get("company", "")).strip():
                    raise ValueError("Sign up could not be completed.")
                password = str(payload.get("password", ""))
                name = str(payload.get("name", payload.get("full_name", payload.get("fullName", ""))))
                confirm_password = str(payload.get("confirm_password", payload.get("confirmPassword", "")))
                if confirm_password and confirm_password != password:
                    raise ValueError("Passwords do not match.")
                user = create_user(str(payload.get("email", "")), password, name=name)
                assign_ownerless_runs_to_user(user)
                token = create_session(user["id"])
            except json.JSONDecodeError:
                self.send_json({"error": "Invalid JSON body."}, status=HTTPStatus.BAD_REQUEST)
                return
            except ValueError as exc:
                self.send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
                return
            self.send_json({"user": user_payload(user)}, headers={"Set-Cookie": self.session_cookie_header(token)})
            return

        if parsed.path == "/api/auth/login":
            try:
                payload = self.read_json_body()
                user = authenticate_user(str(payload.get("email", "")), str(payload.get("password", "")))
                assign_ownerless_runs_to_user(user)
                token = create_session(user["id"])
            except json.JSONDecodeError:
                self.send_json({"error": "Invalid JSON body."}, status=HTTPStatus.BAD_REQUEST)
                return
            except ValueError as exc:
                self.send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
                return
            self.send_json({"user": user_payload(user)}, headers={"Set-Cookie": self.session_cookie_header(token)})
            return

        if parsed.path == "/api/auth/logout":
            cookie = self.cookies()
            token = cookie.get(SESSION_COOKIE_NAME)
            if token:
                delete_session(token.value)
            self.send_json({"ok": True}, headers={"Set-Cookie": self.session_cookie_header("", expires=True)})
            return

        if parsed.path == "/api/billing/quote":
            try:
                payload = self.read_json_body()
                quote = billing_quote_from_payload(payload, self.current_user())
            except json.JSONDecodeError:
                self.send_json({"error": "Invalid JSON body."}, status=HTTPStatus.BAD_REQUEST)
                return
            except PermissionError as exc:
                self.send_json({"error": str(exc)}, status=HTTPStatus.UNAUTHORIZED)
                return
            except RuntimeError as exc:
                self.send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
                return
            except ValueError as exc:
                self.send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
                return
            self.send_json(quote)
            return

        if parsed.path == "/api/billing/checkout":
            user = self.current_user()
            if not user:
                self.send_json({"error": "You must be logged in."}, status=HTTPStatus.UNAUTHORIZED)
                return
            try:
                payload = self.read_json_body()
                plan_id = str(payload.get("planId", payload.get("plan_id", ""))).strip()
                if not plan_id:
                    raise ValueError("Choose a pricing option first.")
                checkout = billing.create_checkout_session(user, plan_id, app_base_url(self))
            except json.JSONDecodeError:
                self.send_json({"error": "Invalid JSON body."}, status=HTTPStatus.BAD_REQUEST)
                return
            except RuntimeError as exc:
                self.send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
                return
            except ValueError as exc:
                self.send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
                return
            self.send_json(checkout)
            return

        if parsed.path == "/api/billing/portal":
            user = self.current_user()
            if not user:
                self.send_json({"error": "You must be logged in."}, status=HTTPStatus.UNAUTHORIZED)
                return
            try:
                session = billing.create_billing_portal_session(user, app_base_url(self))
            except RuntimeError as exc:
                self.send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
                return
            except ValueError as exc:
                self.send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
                return
            self.send_json(session)
            return

        if parsed.path == "/api/stripe/webhook":
            try:
                raw = self.read_raw_body()
                signature = str(self.headers.get("Stripe-Signature", "")).strip()
                result = billing.handle_stripe_webhook(raw, signature)
            except RuntimeError as exc:
                self.send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
                return
            except ValueError as exc:
                self.send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
                return
            self.send_json(result)
            return

        if parsed.path == "/api/account/password":
            user = self.current_user()
            if not user:
                self.send_json({"error": "You must be logged in."}, status=HTTPStatus.UNAUTHORIZED)
                return
            try:
                payload = self.read_json_body()
                current_password = str(payload.get("current_password", payload.get("currentPassword", "")))
                new_password = str(payload.get("new_password", payload.get("newPassword", "")))
                confirm_password = str(payload.get("confirm_password", payload.get("confirmPassword", "")))
                if new_password != confirm_password:
                    raise ValueError("New passwords do not match.")
                change_user_password(user["id"], current_password, new_password)
            except json.JSONDecodeError:
                self.send_json({"error": "Invalid JSON body."}, status=HTTPStatus.BAD_REQUEST)
                return
            except ValueError as exc:
                self.send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
                return
            self.send_json({"ok": True})
            return

        if parsed.path == "/api/conversations":
            try:
                payload = self.read_json_body()
                conversation = start_new_conversation(payload, self.current_user())
            except json.JSONDecodeError:
                self.send_json({"error": "Invalid JSON body."}, status=HTTPStatus.BAD_REQUEST)
                return
            except PermissionError as exc:
                self.send_json({"error": str(exc)}, status=HTTPStatus.UNAUTHORIZED)
                return
            except ValueError as exc:
                self.send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
                return
            self.send_json(conversation, status=HTTPStatus.CREATED)
            return

        if parsed.path.startswith("/api/conversations/") and parsed.path.endswith("/resume"):
            parts = [part for part in parsed.path.split("/") if part]
            if len(parts) == 4:
                run_id = parts[2]
                try:
                    payload = self.read_json_body()
                    conversation = resume_conversation(run_id, payload, self.current_user())
                except json.JSONDecodeError:
                    self.send_json({"error": "Invalid JSON body."}, status=HTTPStatus.BAD_REQUEST)
                    return
                except FileNotFoundError:
                    self.send_json({"error": "Conversation not found."}, status=HTTPStatus.NOT_FOUND)
                    return
                except PermissionError as exc:
                    status = HTTPStatus.UNAUTHORIZED if "logged in" in str(exc).lower() else HTTPStatus.FORBIDDEN
                    self.send_json({"error": str(exc)}, status=status)
                    return
                except ValueError as exc:
                    self.send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
                    return
                self.send_json(conversation)
                return

        self.send_json({"error": "Not found."}, status=HTTPStatus.NOT_FOUND)

    def read_json_body(self) -> Dict[str, Any]:
        raw = self.read_raw_body()
        if not raw:
            return {}
        return json.loads(raw.decode("utf-8"))

    def read_raw_body(self) -> bytes:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0:
            return b""
        raw = self.rfile.read(length)
        return raw or b""

    def send_json(
        self,
        payload: Dict[str, Any],
        status: HTTPStatus = HTTPStatus.OK,
        headers: Optional[Dict[str, Any]] = None,
    ) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        for key, value in (headers or {}).items():
            if isinstance(value, (list, tuple)):
                for item in value:
                    self.send_header(key, str(item))
            else:
                self.send_header(key, str(value))
        self.end_headers()
        self.wfile.write(body)

    def serve_static_file(self, relative_path: str) -> None:
        self.send_static_file(file_response_path(relative_path))

    def send_static_file(self, file_path: Path) -> None:
        mime, _ = mimetypes.guess_type(str(file_path))
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", mime or "application/octet-stream")
        self.end_headers()
        self.wfile.write(file_path.read_bytes())

    def send_html(self, body: bytes, status: HTTPStatus = HTTPStatus.OK) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
        return


def parse_server_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the Pantheon web interface.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    return parser.parse_args()


def main() -> int:
    orch.load_dotenv(APP_ROOT / ".env")
    args = parse_server_args()
    RUNS_ROOT.mkdir(parents=True, exist_ok=True)
    init_auth_db()
    backfill_runs_to_storage()
    server = ThreadingHTTPServer((args.host, args.port), AppHandler)
    print(f"Pantheon running at http://{args.host}:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
