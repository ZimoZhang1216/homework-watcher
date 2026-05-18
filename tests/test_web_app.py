from __future__ import annotations

import tempfile
import unittest
import asyncio
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

    def test_platform_login_manager_uses_async_playwright(self):
        class FakePage:
            def __init__(self):
                self.goto_args = None

            async def goto(self, *args, **kwargs):
                self.goto_args = (args, kwargs)

        class FakeContext:
            def __init__(self):
                self.pages = [FakePage()]
                self.closed = False

            async def new_page(self):
                page = FakePage()
                self.pages.append(page)
                return page

            async def close(self):
                self.closed = True

        class FakeChromium:
            def __init__(self, context):
                self.context = context
                self.launch_kwargs = None

            async def launch_persistent_context(self, **kwargs):
                self.launch_kwargs = kwargs
                return self.context

        class FakePlaywright:
            def __init__(self, context):
                self.chromium = FakeChromium(context)
                self.stopped = False

            async def stop(self):
                self.stopped = True

        class FakePlaywrightFactory:
            def __init__(self, playwright):
                self.playwright = playwright

            async def start(self):
                return self.playwright

        async def run_case():
            with tempfile.TemporaryDirectory() as tmp:
                original_web_dir = web_app.WEB_DIR
                original_loader = web_app.load_async_playwright_for_web
                context = FakeContext()
                playwright = FakePlaywright(context)
                web_app.WEB_DIR = Path(tmp)
                web_app.load_async_playwright_for_web = lambda: (lambda: FakePlaywrightFactory(playwright), Exception)
                try:
                    manager = LoginSessionManager()
                    user = WebUser(
                        id=1,
                        email="demo@example.com",
                        report_email="demo@example.com",
                        created_at="2026-05-18 08:00:00",
                    )
                    await manager.start(user=user, platform="xiaoya")
                    status = manager.status_for(user_id=user.id)
                    await manager.finish(user_id=user.id)
                finally:
                    web_app.WEB_DIR = original_web_dir
                    web_app.load_async_playwright_for_web = original_loader
            return status, context, playwright

        status, context, playwright = asyncio.run(run_case())
        self.assertIsNotNone(status)
        self.assertEqual(status["platform"], "小雅")
        self.assertTrue(context.closed)
        self.assertTrue(playwright.stopped)


if __name__ == "__main__":
    unittest.main()
