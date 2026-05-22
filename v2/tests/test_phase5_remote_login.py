from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from homework_watcher.remote_login import normalize_novnc_url, profile_dir_for_user_platform
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
    )


class Phase5RemoteLoginTests(unittest.TestCase):
    def test_novnc_url_gets_proxy_path_defaults(self) -> None:
        self.assertEqual(
            normalize_novnc_url("http://example.test/vnc/vnc.html"),
            "http://example.test/vnc/vnc.html?autoconnect=1&resize=scale&path=vnc%2Fwebsockify",
        )
        self.assertEqual(
            normalize_novnc_url("http://example.test/vnc/vnc.html?path=websockify"),
            "http://example.test/vnc/vnc.html?path=vnc%2Fwebsockify&autoconnect=1&resize=scale",
        )

    def test_profile_dir_is_user_scoped_for_future_accounts(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            settings = test_settings(tmpdir)
            path = profile_dir_for_user_platform(settings, "user/7", "changjiang-yuketang")
            self.assertEqual(
                path,
                Path(tmpdir) / "profiles" / "users" / "user-7" / "changjiang-yuketang",
            )


if __name__ == "__main__":
    unittest.main()
