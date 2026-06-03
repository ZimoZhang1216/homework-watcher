from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from homework_watcher.app import render_scan_error_panel
from homework_watcher.scan_errors import describe_scan_error_text, format_scan_failure
from homework_watcher.scan_service import ScanService
from homework_watcher.scanners.base import ScannerContext
from homework_watcher.settings import Settings
from homework_watcher.web_scan import ServerScanCommandError


def service_settings(tmpdir: str) -> Settings:
    root = Path(tmpdir)
    return Settings(
        app_version="V2-test",
        database_url=f"sqlite:///{root / 'homework.sqlite3'}",
        config_path=Path("missing-platforms.yaml"),
        debug_dump_dir=root / "debug",
        logs_dir=root / "logs",
        playwright_user_data_dir=root / "profiles",
        host="127.0.0.1",
        port=8080,
    )


class Phase10ScanErrorTests(unittest.TestCase):
    def test_login_expired_error_includes_user_fix_steps(self) -> None:
        advice = describe_scan_error_text(
            "RuntimeError: 小雅登录态可能失效，请先运行 login-xiaoya 手动登录",
            platform_key="xiaoya",
        )

        self.assertEqual(advice.code, "auth_required")
        text = advice.to_text()
        self.assertIn("小雅需要重新登录", text)
        self.assertIn("平台登录", text)
        self.assertIn("扫码", text)

    def test_server_scan_command_prefers_structured_result_error(self) -> None:
        advice = describe_scan_error_text(
            "RuntimeError: 小雅登录态可能失效，请先运行 login-xiaoya 手动登录",
            platform_key="xiaoya",
        )
        error = ServerScanCommandError(
            "服务器扫描命令失败",
            returncode=1,
            stderr="raw subprocess traceback",
            result={
                "scan_id": "scan-test",
                "todos": [],
                "errors": [advice.to_text()],
                "error_details": [advice.to_dict()],
                "platform_summaries": {},
            },
        )

        message = str(error)

        self.assertIn("小雅需要重新登录", message)
        self.assertNotIn("服务器扫描命令失败 exit=1", message)

    def test_course_scan_empty_result_is_actionable(self) -> None:
        message = format_scan_failure(
            {
                "scan_id": "course-scan-test",
                "mode": "courses",
                "courses": [],
                "errors": [],
                "platform_summaries": {
                    "xiaoya": {
                        "platform_label": "小雅",
                        "status": "needs_action",
                        "message": "小雅课程扫描没有发现可保存课程。请先在“平台登录”重新登录小雅，确认课程页能看到课程，再重新点击“扫描课程”。",
                    }
                },
            }
        )

        self.assertIn("小雅课程扫描没有发现课程", message)
        self.assertIn("重新登录小雅", message)

    def test_scan_service_records_structured_error_details(self) -> None:
        class LoginExpiredScanner:
            platform_key = "xiaoya"

            def scan(self, context: ScannerContext):
                context.metadata["xiaoya"] = {
                    "platform_label": "小雅",
                    "status": "failed",
                    "message": "小雅登录态可能失效，请先运行 login-xiaoya 手动登录",
                }
                raise RuntimeError("小雅登录态可能失效，请先运行 login-xiaoya 手动登录")

        with tempfile.TemporaryDirectory() as tmpdir:
            result = ScanService(
                service_settings(tmpdir),
                scanners=[LoginExpiredScanner()],
            ).run_scan(platforms=["xiaoya"])

        self.assertEqual(result.error_details[0]["code"], "auth_required")
        self.assertIn("小雅需要重新登录", result.errors[0])
        self.assertIn("xiaoya", result.platform_summaries)

    def test_render_scan_error_panel_preserves_readable_text(self) -> None:
        panel = render_scan_error_panel("小雅需要重新登录\n处理方法：重新登录后再扫描")

        self.assertIn('role="alert"', panel)
        self.assertIn("上次扫描失败", panel)
        self.assertIn("处理方法", panel)


if __name__ == "__main__":
    unittest.main()
