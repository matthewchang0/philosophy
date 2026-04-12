#!/usr/bin/env python3
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

import orchestrator as orch
import pantheon_storage as storage
import webapp


LOCAL_DB_PATH = APP_ROOT / "pantheon_auth.db"
LOCAL_RUNS_ROOT = APP_ROOT / "runs"


def migrate_users() -> int:
    if not LOCAL_DB_PATH.exists():
        return 0
    connection = sqlite3.connect(LOCAL_DB_PATH)
    connection.row_factory = sqlite3.Row
    count = 0
    try:
        rows = connection.execute(
            "SELECT id, email, name, avatar_url, auth_provider, password_salt, password_hash, created_at FROM users"
        ).fetchall()
        for row in rows:
            existing = storage.fetch_user_by_email(str(row["email"]))
            if existing:
                storage.update_user(
                    existing["id"],
                    {
                        "name": row["name"] or existing.get("name", ""),
                        "avatar_url": row["avatar_url"] or existing.get("avatar_url", ""),
                        "auth_provider": row["auth_provider"] or existing.get("auth_provider", "local"),
                        "password_salt": row["password_salt"] or existing["password_salt"],
                        "password_hash": row["password_hash"] or existing["password_hash"],
                    },
                )
            else:
                storage.insert_user(
                    {
                        "id": str(row["id"]),
                        "email": str(row["email"]),
                        "name": str(row["name"] or ""),
                        "avatar_url": str(row["avatar_url"] or ""),
                        "auth_provider": str(row["auth_provider"] or "local"),
                        "password_salt": str(row["password_salt"]),
                        "password_hash": str(row["password_hash"]),
                        "created_at": str(row["created_at"]),
                        "created_source": "local",
                        "created_host": "legacy-local",
                        "last_login_at": "",
                        "last_login_source": "",
                    }
                )
            count += 1
    finally:
        connection.close()
    return count


def migrate_runs() -> int:
    if not LOCAL_RUNS_ROOT.exists():
        return 0
    count = 0
    for run_dir in LOCAL_RUNS_ROOT.iterdir():
        if not webapp.is_run_dir(run_dir):
            continue
        webapp.sync_run_dir_to_storage(run_dir)
        count += 1
    return count


def main() -> int:
    orch.load_dotenv(APP_ROOT / ".env")
    if not storage.storage_enabled():
        raise SystemExit("DATABASE_URL is required for migration.")
    storage.init_storage()
    users = migrate_users()
    runs = migrate_runs()
    print(f"Migrated users: {users}")
    print(f"Migrated runs: {runs}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
