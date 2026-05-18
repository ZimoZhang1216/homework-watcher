from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

try:
    import homework_watcher.web_app as web_app
    from homework_watcher.web_app import LoginSessionManager, WebStore, WebUser
except ModuleNotFoundError as exc:
    if exc.name != "fastapi":
        raise
    web_app = None


@unittest.skipIf(web_app is None, "FastAPI is not installed")
class WebAppTest(unittest.TestCase):
    def test_public_and_dashboard_render(self):
        with tempfile.TemporaryDirectory() as tmp:
            original_web_dir = web_app.WEB_DIR
            web_app.WEB_DIR = Path(tmp)
            try:
                store = WebStore(Path(tmp) / "web.db")
                user = WebUser(
                    id=1,
                    email="demo@example.com",
                    report_email="demo@example.com",
                    created_at="2026-05-18 08:00:00",
                )

                public_html = web_app.public_home()
                dashboard_html = web_app.dashboard_page(user, store, LoginSessionManager()).body.decode("utf-8")
            finally:
                web_app.WEB_DIR = original_web_dir

        self.assertIn("作业日报托管台", public_html)
        self.assertIn("auth-shell", public_html)
        self.assertIn("metric-grid", dashboard_html)
        self.assertIn("当前待办", dashboard_html)
        self.assertIn("平台登录态", dashboard_html)

    def test_web_store_can_return_created_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = WebStore(Path(tmp) / "web.db")
            user = store.create_user(
                email="demo@example.com",
                report_email="demo@example.com",
                password="long-enough-password",
            )
            job = store.create_job(user_id=user.id, kind="scan")

        self.assertEqual(user.email, "demo@example.com")
        self.assertEqual(job.kind, "scan")
        self.assertEqual(job.status, "running")


if __name__ == "__main__":
    unittest.main()
