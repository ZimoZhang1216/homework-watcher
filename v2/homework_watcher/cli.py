from __future__ import annotations

import argparse
import json

from .app import app
from .database import assignment_to_dict, create_session_factory, init_db, list_assignments
from .git_utils import git_commit
from .settings import load_settings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="homework-watcher-v2")
    subparsers = parser.add_subparsers(dest="command")

    health_parser = subparsers.add_parser("health", help="输出服务健康信息")
    health_parser.set_defaults(handler=cmd_health)

    db_list_parser = subparsers.add_parser("db-list", help="输出 assignments 表记录")
    db_list_parser.set_defaults(handler=cmd_db_list)

    args = parser.parse_args(argv)
    if not hasattr(args, "handler"):
        parser.print_help()
        return 0
    return args.handler(args)


def cmd_health(_args) -> int:
    settings = load_settings()
    print(
        json.dumps(
            {
                "ok": True,
                "version": settings.app_version,
                "git_commit": git_commit(),
                "database_path": str(settings.database_path),
                "config_path": str(settings.config_path),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def cmd_db_list(_args) -> int:
    settings = load_settings()
    init_db(settings)
    session_factory = create_session_factory(settings)
    with session_factory() as session:
        items = [assignment_to_dict(item) for item in list_assignments(session)]
    print(json.dumps(items, ensure_ascii=False, indent=2))
    return 0


__all__ = ["app", "main"]


if __name__ == "__main__":
    raise SystemExit(main())
