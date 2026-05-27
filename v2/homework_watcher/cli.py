from __future__ import annotations

import argparse
import json
from dataclasses import replace

from .config_loader import load_platform_configs
from .database import assignment_to_dict, create_session_factory, init_db, list_assignments, list_todos
from .git_utils import git_commit
from .scan_service import ScanService, scanner_source_path
from .scanners.xiaoya import XiaoyaScanner, login_xiaoya
from .scanners.xiaoya_discovery import xiaoya_course_to_dict
from .settings import load_settings, resolve_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="homework-watcher-v2")
    subparsers = parser.add_subparsers(dest="command")

    health_parser = subparsers.add_parser("health", help="输出服务健康信息")
    health_parser.set_defaults(handler=cmd_health)

    db_list_parser = subparsers.add_parser("db-list", help="输出 assignments 表记录")
    db_list_parser.add_argument("--user", default="default", help="账号学号，默认 default")
    db_list_parser.set_defaults(handler=cmd_db_list)

    scan_parser = subparsers.add_parser("scan", help="执行统一扫描服务")
    scan_parser.add_argument("--platform", action="append", dest="platforms", help="限制扫描平台，可重复")
    scan_parser.add_argument("--user", default="default", help="账号学号，默认 default")
    scan_parser.set_defaults(handler=cmd_scan)

    login_xiaoya_parser = subparsers.add_parser("login-xiaoya", help="打开小雅登录浏览器并保存登录态")
    login_xiaoya_parser.add_argument("--user", default="default", help="账号学号，默认 default")
    login_xiaoya_parser.set_defaults(handler=cmd_login_xiaoya)

    discover_xiaoya_parser = subparsers.add_parser(
        "discover-xiaoya-courses", help="只发现小雅课程，不写 assignments"
    )
    discover_xiaoya_parser.add_argument("--user", default="default", help="账号学号，默认 default")
    discover_xiaoya_parser.set_defaults(handler=cmd_discover_xiaoya_courses)

    scan_xiaoya_auto_parser = subparsers.add_parser(
        "scan-xiaoya-auto", help="执行完整小雅自动课程发现和任务页扫描"
    )
    scan_xiaoya_auto_parser.add_argument("--user", default="default", help="账号学号，默认 default")
    scan_xiaoya_auto_parser.set_defaults(handler=cmd_scan_xiaoya_auto)

    diagnose_xiaoya_parser = subparsers.add_parser(
        "diagnose-xiaoya", help="诊断小雅课程发现、任务抓取、入库和待办"
    )
    diagnose_xiaoya_parser.add_argument("--user", default="default", help="账号学号，默认 default")
    diagnose_xiaoya_parser.set_defaults(handler=cmd_diagnose_xiaoya)

    diagnose_xiaoya_click_parser = subparsers.add_parser(
        "diagnose-xiaoya-click-discovery", help="只通过点击课程卡片诊断小雅 course_id 发现"
    )
    diagnose_xiaoya_click_parser.add_argument("--user", default="default", help="账号学号，默认 default")
    diagnose_xiaoya_click_parser.set_defaults(handler=cmd_diagnose_xiaoya_click_discovery)

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
                "debug_dump_dir": str(settings.debug_dump_dir),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def cmd_db_list(args) -> int:
    settings = load_settings()
    init_db(settings)
    session_factory = create_session_factory(settings)
    with session_factory() as session:
        items = [assignment_to_dict(item) for item in list_assignments(session, owner_key=args.user)]
    print(json.dumps(items, ensure_ascii=False, indent=2))
    return 0


def cmd_scan(args) -> int:
    settings = load_settings()
    result = ScanService(settings, user_key=args.user).run_scan(platforms=args.platforms)
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    return 0 if not result.errors else 1


def cmd_login_xiaoya(args) -> int:
    login_xiaoya(load_settings(), user_key=args.user)
    return 0


def cmd_scan_xiaoya_auto(_args) -> int:
    return run_xiaoya_scan_diagnostic(_args)


def cmd_diagnose_xiaoya(args) -> int:
    return run_xiaoya_scan_diagnostic(args)


def cmd_discover_xiaoya_courses(args) -> int:
    settings = load_settings()
    config = load_platform_configs(settings.config_path).get("xiaoya")
    if config is None:
        print("xiaoya platform config not found")
        return 1
    courses = XiaoyaScanner(settings).discover_courses_only(
        config,
        user_key=args.user,
        emit=print,
    )
    rows = [xiaoya_course_to_dict(course) for course in courses]
    logs_dir = resolve_path(settings.logs_dir)
    logs_dir.mkdir(parents=True, exist_ok=True)
    output_path = logs_dir / "xiaoya_discovered_courses_latest.json"
    output_path.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    print_course_table(rows)
    print(f"saved_json={output_path}")
    return 0 if courses else 1


def cmd_diagnose_xiaoya_click_discovery(args) -> int:
    settings = load_settings()
    config = load_platform_configs(settings.config_path).get("xiaoya")
    if config is None:
        print("xiaoya platform config not found")
        return 1
    courses = XiaoyaScanner(settings).discover_courses_by_click_url_only(
        config,
        user_key=args.user,
        emit=print,
    )
    print_course_table([xiaoya_course_to_dict(course) for course in courses])
    return 0 if courses else 1


def run_xiaoya_scan_diagnostic(args) -> int:
    settings = load_settings()
    service = ScanService(settings, user_key=args.user)
    config = service.configs.get("xiaoya")
    if config is not None:
        service.configs["xiaoya"] = replace(config, enabled=True, auto_discover_courses=True)
    result = service.run_scan(platforms=["xiaoya"])
    init_db(settings)
    session_factory = create_session_factory(settings)
    with session_factory() as session:
        assignments = [assignment_to_dict(item) for item in list_assignments(session, owner_key=args.user)]
        todos = [assignment_to_dict(item) for item in list_todos(session, owner_key=args.user)]

    xiaoya_assignments = [item for item in assignments if item["platform"] == "小雅"]
    xiaoya_todos = [item for item in todos if item["platform"] == "小雅"]
    diagnosis = diagnose_xiaoya_result(
        result=result,
        xiaoya_assignments=xiaoya_assignments,
        xiaoya_todos=xiaoya_todos,
    )
    output = {
        "version": settings.app_version,
        "git_commit": git_commit(),
        "scanner_file": scanner_source_path(XiaoyaScanner),
        "debug_dump_dir": str(settings.debug_dump_dir),
        "diagnosis": diagnosis,
        "xiaoya_summary": result.platform_summaries.get("xiaoya", {}),
        "scan": result.to_dict(),
        "xiaoya_assignment_count": len(xiaoya_assignments),
        "xiaoya_todo_count": len(xiaoya_todos),
        "xiaoya_assignments": xiaoya_assignments,
        "xiaoya_todos": xiaoya_todos,
    }
    print(json.dumps(output, ensure_ascii=False, indent=2))
    print(diagnosis)
    return 0 if diagnosis == "PASS" else 1


def diagnose_xiaoya_result(
    *,
    result,
    xiaoya_assignments: list[dict[str, object]],
    xiaoya_todos: list[dict[str, object]],
) -> str:
    summary = result.platform_summaries.get("xiaoya", {})
    merged_count = int(summary.get("merged_courses_count") or 0)
    scanned_count = int(summary.get("scanned_courses_count") or 0)
    failed_count = int(summary.get("failed_courses_count") or 0)
    parsed_count = int(summary.get("parsed_assignments_count") or 0)
    if result.errors and not summary:
        return "FAIL_AT_PAGE_LOAD"
    if merged_count == 0:
        return "FAIL_AT_DISCOVERY"
    if scanned_count == 0 and failed_count:
        return "FAIL_AT_PAGE_LOAD"
    if parsed_count == 0:
        return "FAIL_AT_PARSE"
    if not xiaoya_assignments or any(is_xiaoya_fake_summary_row(item) for item in xiaoya_assignments):
        return "FAIL_AT_DB"
    if not xiaoya_todos:
        return "FAIL_AT_TODO"
    if any(not is_valid_todo_row(item) or is_xiaoya_fake_summary_row(item) for item in xiaoya_todos):
        return "FAIL_AT_WEB"
    if result.errors:
        return "FAIL_AT_PAGE_LOAD"
    return "PASS"


def is_xiaoya_fake_summary_row(item: dict[str, object]) -> bool:
    return (
        item.get("platform") == "小雅"
        and compact_text(str(item.get("course") or "")) == compact_text(str(item.get("title") or ""))
        and item.get("status_normalized") == "unknown"
    )


def is_valid_todo_row(item: dict[str, object]) -> bool:
    return item.get("status_normalized") in {"in_progress", "pending"}


def compact_text(value: str) -> str:
    return "".join(str(value or "").split())


def print_course_table(rows: list[dict[str, str]]) -> None:
    columns = ["course", "course_id", "source", "task_url"]
    if not rows:
        print("\t".join(columns))
        return
    print("\t".join(columns))
    for row in rows:
        print("\t".join(str(row.get(column) or "") for column in columns))


__all__ = ["main"]


if __name__ == "__main__":
    raise SystemExit(main())
