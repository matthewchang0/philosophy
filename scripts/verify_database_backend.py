#!/usr/bin/env python3
from __future__ import annotations

import os
import sys
from pathlib import Path
from uuid import uuid4

from sqlalchemy import create_engine, inspect, text

APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

import orchestrator as orch
import pantheon_storage as storage
import webapp


REQUIRED_TABLES = {"users", "sessions", "auth_events", "conversations"}


def main() -> int:
    orch.load_dotenv(APP_ROOT / ".env")
    if not storage.storage_enabled():
        raise SystemExit("DATABASE_URL is required.")

    storage.init_storage()
    engine = create_engine(storage.database_url(), future=True, pool_pre_ping=True)

    with engine.begin() as connection:
        inspector = inspect(connection)
        tables = set(inspector.get_table_names())
        missing = sorted(REQUIRED_TABLES - tables)
        if missing:
            raise SystemExit(f"Missing required tables: {', '.join(missing)}")

    suffix = uuid4().hex[:10]
    email = f"pantheon-smoke-{suffix}@example.com"
    password = "TempPass123!"
    user = None
    session_token = None

    try:
        user = webapp.create_user(email, password, name="Pantheon Smoke")
        authed_user = webapp.authenticate_user(email, password)
        session_token = webapp.create_session(user["id"])
        session_user = webapp.user_for_session_token(session_token)

        if authed_user.get("email") != email:
            raise SystemExit("Authentication smoke test failed.")
        if not session_user or session_user.get("email") != email:
            raise SystemExit("Session smoke test failed.")

        print("Database backend verified.")
        print(f"Tables present: {', '.join(sorted(REQUIRED_TABLES))}")
        print("Auth flow smoke test: passed")
        return 0
    finally:
        if user:
            with engine.begin() as connection:
                if session_token:
                    connection.execute(text("DELETE FROM sessions WHERE token = :token"), {"token": session_token})
                connection.execute(text("DELETE FROM auth_events WHERE user_id = :user_id"), {"user_id": user["id"]})
                connection.execute(text("DELETE FROM users WHERE id = :user_id"), {"user_id": user["id"]})


if __name__ == "__main__":
    raise SystemExit(main())
