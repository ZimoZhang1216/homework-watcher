from __future__ import annotations

import asyncio
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from homework_watcher.app import create_app
from homework_watcher.auth import (
    AuthError,
    authenticate_user,
    create_session_token,
    create_user,
    validate_username,
    read_session_username,
    verify_password,
)
from homework_watcher.database import create_session_factory, init_db
from homework_watcher.settings import Settings


def test_settings(tmpdir: str) -> Settings:
    root = Path(tmpdir)
    return Settings(
        app_version="V2-test",
        database_url=f"sqlite:///{root / 'homework.sqlite3'}",
        config_path=root / "platforms.yaml",
        debug_dump_dir=root / "debug",
        logs_dir=root / "logs",
        playwright_user_data_dir=root / "profiles",
        host="127.0.0.1",
        port=8080,
        session_secret="test-secret",
    )


class Phase7AuthTests(unittest.TestCase):
    def test_create_and_authenticate_user(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            settings = test_settings(tmpdir)
            init_db(settings)
            session_factory = create_session_factory(settings)
            with session_factory() as session:
                user = create_user(session, username="Alice_1", password="password123", display_name="Alice")
                self.assertEqual(user.username, "alice_1")
                self.assertTrue(verify_password("password123", user.password_hash))

            with session_factory() as session:
                self.assertIsNotNone(authenticate_user(session, username="alice_1", password="password123"))
                self.assertIsNone(authenticate_user(session, username="alice_1", password="wrong-pass"))

    def test_reject_short_password(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            settings = test_settings(tmpdir)
            init_db(settings)
            session_factory = create_session_factory(settings)
            with session_factory() as session:
                with self.assertRaises(AuthError):
                    create_user(session, username="alice", password="short")

    def test_invalid_account_message_uses_student_id_wording(self) -> None:
        with self.assertRaisesRegex(AuthError, "学号"):
            validate_username("!")

    def test_signed_session_token(self) -> None:
        token = create_session_token("alice", "secret", now=1000)

        self.assertEqual(read_session_username(token, "secret", now=1001), "alice")
        self.assertIsNone(read_session_username(token, "wrong-secret", now=1001))
        self.assertIsNone(read_session_username(token, "secret", now=1000 + 15 * 24 * 60 * 60))

    def test_successful_web_login_starts_background_task_scan(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            env = {
                "DATABASE_URL": f"sqlite:///{root / 'homework.sqlite3'}",
                "CONFIG_PATH": str(root / "platforms.yaml"),
                "DEBUG_DUMP_DIR": str(root / "debug"),
                "LOGS_DIR": str(root / "logs"),
                "PLAYWRIGHT_USER_DATA_DIR": str(root / "profiles"),
                "APP_SECRET_KEY": "test-secret",
                "PORT": "8080",
            }
            with patch.dict(os.environ, env, clear=False):
                app = create_app()
                settings = test_settings(tmpdir)
                init_db(settings)
                session_factory = create_session_factory(settings)
                with session_factory() as session:
                    create_user(session, username="alice", password="password123", display_name="Alice")

                called = threading.Event()
                call_args: dict[str, object] = {}

                def fake_scan_command(settings, *, owner_key: str, mode: str = "tasks", emit=None, check_cancelled=None):
                    call_args.update({"owner_key": owner_key, "mode": mode})
                    called.set()
                    return {"scan_id": "auto-login-scan", "todos": [], "platform_summaries": {}}

                class FakeLoginRequest:
                    async def body(self) -> bytes:
                        return b"username=alice&password=password123"

                with patch("homework_watcher.app.run_server_scan_command", side_effect=fake_scan_command):
                    login_endpoint = next(
                        route.endpoint
                        for route in app.routes
                        if getattr(route, "path", "") == "/login" and "POST" in getattr(route, "methods", set())
                    )
                    response = asyncio.run(login_endpoint(FakeLoginRequest()))
                    did_start_scan = called.wait(2)

                self.assertEqual(response.status_code, 303)
                self.assertTrue(did_start_scan, "login did not start a background scan")
                self.assertEqual(call_args, {"owner_key": "alice", "mode": "tasks"})
                cookie = response.headers.get("set-cookie", "")
                self.assertIn("hw_v2_session=", cookie)
                self.assertIn("HttpOnly", cookie)
                self.assertNotIn("Max-Age", cookie)
                self.assertNotIn("expires=", cookie.lower())


if __name__ == "__main__":
    unittest.main()
