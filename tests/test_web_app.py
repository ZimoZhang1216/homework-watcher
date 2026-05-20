from __future__ import annotations

import tempfile
import unittest
import asyncio
from datetime import datetime
from pathlib import Path

try:
    import homework_watcher.web_app as web_app
    from homework_watcher.db import HomeworkDB
    from homework_watcher.platforms.base import PlatformAssignment
    from homework_watcher.recurring_assignments import materialize_recurring_assignments
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
            store.update_job_progress(job_id=job.id, message="扫描小雅", progress=45)
            updated_job = store.get_job(job.id)

        self.assertEqual(user.email, "demo@example.com")
        self.assertEqual(job.kind, "scan")
        self.assertEqual(job.status, "running")
        self.assertEqual(updated_job.message, "扫描小雅")
        self.assertEqual(updated_job.progress, 45)

    def test_recurring_assignments_can_be_marked_done_from_dashboard(self):
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
                db = HomeworkDB(web_app.user_homework_db_path(user.id))
                try:
                    materialize_recurring_assignments(db, now=datetime(2026, 5, 18, 8, 0), horizon_days=7)
                    assignment = db.list_assignments()[0]
                finally:
                    db.close()

                html = web_app.dashboard_page(user, store, LoginSessionManager()).body.decode("utf-8")
                self.assertIn(f"/assignments/{assignment.id}/done", html)

                db = HomeworkDB(web_app.user_homework_db_path(user.id))
                try:
                    db.mark_done(assignment.id)
                    active_ids = [item.id for item in db.list_assignments()]
                finally:
                    db.close()
            finally:
                web_app.WEB_DIR = original_web_dir

        self.assertNotIn(assignment.id, active_ids)

    def test_web_scan_marks_platform_done_assignments_completed(self):
        class FakeAdapter:
            platform_name = "小雅"
            slug = "xiaoya"

            def __init__(self, *, profile_root):
                self.profile_root = profile_root

            def fetch_assignments(self, *, headless=True, progress=None):
                if progress is not None:
                    progress("小雅：扫描课程 1/1 测试课程")
                return [
                    PlatformAssignment(
                        title="已完成旧作业",
                        course="测试课程",
                        platform="小雅",
                        due_at=datetime(2026, 5, 14, 23, 59),
                        status="已完成",
                        url="https://example.test/task",
                    )
                ]

        with tempfile.TemporaryDirectory() as tmp:
            original_web_dir = web_app.WEB_DIR
            original_slugs = web_app.canonical_slugs
            original_adapters = web_app.ADAPTER_CLASSES
            messages = []
            web_app.WEB_DIR = Path(tmp)
            web_app.canonical_slugs = lambda: ["xiaoya"]
            web_app.ADAPTER_CLASSES = {"xiaoya": FakeAdapter}
            try:
                user = WebUser(
                    id=1,
                    email="demo@example.com",
                    report_email="demo@example.com",
                    created_at="2026-05-18 08:00:00",
                )
                web_app.scan_user_homework(user, progress=lambda message, percent=None: messages.append((message, percent)))
                db = HomeworkDB(web_app.user_homework_db_path(user.id))
                try:
                    all_items = db.list_assignments(include_done=True)
                    active_items = db.list_assignments()
                finally:
                    db.close()
            finally:
                web_app.WEB_DIR = original_web_dir
                web_app.canonical_slugs = original_slugs
                web_app.ADAPTER_CLASSES = original_adapters

        done_items = [item for item in all_items if item.title == "已完成旧作业"]
        active_titles = {item.title for item in active_items}
        self.assertEqual(len(done_items), 1)
        self.assertTrue(done_items[0].is_done)
        self.assertNotIn("已完成旧作业", active_titles)
        self.assertTrue(any(percent == 100 for _, percent in messages))

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

    def test_platform_login_manager_reuses_and_expires_sessions(self):
        class FakePage:
            async def goto(self, *args, **kwargs):
                pass

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
            def __init__(self, contexts):
                self.contexts = contexts
                self.launch_count = 0

            async def launch_persistent_context(self, **kwargs):
                self.launch_count += 1
                context = FakeContext()
                self.contexts.append(context)
                return context

        class FakePlaywright:
            def __init__(self, contexts):
                self.chromium = FakeChromium(contexts)
                self.stopped = False

            async def stop(self):
                self.stopped = True

        class FakePlaywrightFactory:
            def __init__(self, playwrights, contexts):
                self.playwrights = playwrights
                self.contexts = contexts

            async def start(self):
                playwright = FakePlaywright(self.contexts)
                self.playwrights.append(playwright)
                return playwright

        async def run_case():
            with tempfile.TemporaryDirectory() as tmp:
                original_web_dir = web_app.WEB_DIR
                original_loader = web_app.load_async_playwright_for_web
                playwrights = []
                contexts = []
                web_app.WEB_DIR = Path(tmp)
                web_app.load_async_playwright_for_web = (
                    lambda: (lambda: FakePlaywrightFactory(playwrights, contexts), Exception)
                )
                try:
                    manager = LoginSessionManager()
                    user = WebUser(
                        id=1,
                        email="demo@example.com",
                        report_email="demo@example.com",
                        created_at="2026-05-18 08:00:00",
                    )
                    other_user = WebUser(
                        id=2,
                        email="other@example.com",
                        report_email="other@example.com",
                        created_at="2026-05-18 08:00:00",
                    )
                    await manager.start(user=user, platform="xiaoya")
                    await manager.start(user=user, platform="changjiang-yuketang")
                    launch_count_after_reuse = len(playwrights)
                    with manager.lock:
                        manager.active["started_monotonic"] -= web_app.LOGIN_SESSION_TTL_SECONDS + 1
                    await manager.start(user=other_user, platform="xiaoya")
                    status = manager.status_for(user_id=other_user.id)
                finally:
                    web_app.WEB_DIR = original_web_dir
                    web_app.load_async_playwright_for_web = original_loader
            return launch_count_after_reuse, status, contexts, playwrights

        launch_count_after_reuse, status, contexts, playwrights = asyncio.run(run_case())
        self.assertEqual(launch_count_after_reuse, 1)
        self.assertEqual(len(playwrights), 2)
        self.assertTrue(contexts[0].closed)
        self.assertTrue(playwrights[0].stopped)
        self.assertEqual(status["platform"], "小雅")
        self.assertFalse(contexts[1].closed)


if __name__ == "__main__":
    unittest.main()
