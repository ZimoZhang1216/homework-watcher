from __future__ import annotations

import argparse
import json

from .app import app
from .database import assignment_to_dict, create_session_factory, init_db, list_assignments, list_todos
from .git_utils import git_commit
from .scan_service import ScanService, scanner_source_path
from .scanners.xiaoya import XiaoyaScanner, login_xiaoya
from .settings import load_settings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="homework-watcher-v2")
    subparsers = parser.add_subparsers(dest="command")

    health_parser = subparsers.add_parser("health", help="输出服务健康信息")
    health_parser.set_defaults(handler=cmd_health)

    db_list_parser = subparsers.add_parser("db-list", help="输出 assignments 表记录")
    db_list_parser.set_defaults(handler=cmd_db_list)

    scan_parser = subparsers.add_parser("scan", help="执行统一扫描服务")
    scan_parser.add_argument("--platform", action="append", dest="platforms", help="限制扫描平台，可重复")
    scan_parser.set_defaults(handler=cmd_scan)

    login_xiaoya_parser = subparsers.add_parser("login-xiaoya", help="打开小雅登录浏览器并保存登录态")
    login_xiaoya_parser.set_defaults(handler=cmd_login_xiaoya)

    scan_known_xiaoya_parser = subparsers.add_parser(
        "scan-known-xiaoya", help="扫描小雅配置的 known_courses 并执行结构化学断言"
    )
    scan_known_xiaoya_parser.set_defaults(handler=cmd_scan_known_xiaoya)

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


def cmd_scan(args) -> int:
    settings = load_settings()
    result = ScanService(settings).run_scan(platforms=args.platforms)
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    return 0 if not result.errors else 1


def cmd_login_xiaoya(_args) -> int:
    login_xiaoya(load_settings())
    return 0


def cmd_scan_known_xiaoya(_args) -> int:
    settings = load_settings()
    result = ScanService(settings).run_scan(platforms=["xiaoya"])
    init_db(settings)
    session_factory = create_session_factory(settings)
    with session_factory() as session:
        assignments = [assignment_to_dict(item) for item in list_assignments(session)]
        todos = [assignment_to_dict(item) for item in list_todos(session)]

    checks = {
        "db_has_zuoye_08": any(item["platform"] == "小雅" and item["title"] == "作业-08" for item in assignments),
        "db_has_lattice": any(
            item["platform"] == "小雅" and "实习2" in str(item["title"]) and "点阵理论" in str(item["title"])
            for item in assignments
        ),
        "todo_has_zuoye_08": any(item["platform"] == "小雅" and item["title"] == "作业-08" for item in todos),
        "todo_has_lattice": any(
            item["platform"] == "小雅" and "实习2" in str(item["title"]) and "点阵理论" in str(item["title"])
            for item in todos
        ),
        "todo_has_completed_symmetry": any(
            item["platform"] == "小雅" and item["title"] == "实习1 分子对称性" for item in todos
        ),
        "todo_has_fake_structure_summary": any(
            item["platform"] == "小雅"
            and item["course"] == "结构化学"
            and item["title"] == "结构化学"
            and str(item["due_at"]).startswith("2026-07-31")
            for item in todos
        ),
    }
    ok = (
        checks["db_has_zuoye_08"]
        and checks["db_has_lattice"]
        and checks["todo_has_zuoye_08"]
        and checks["todo_has_lattice"]
        and not checks["todo_has_completed_symmetry"]
        and not checks["todo_has_fake_structure_summary"]
    )
    output = {
        "version": settings.app_version,
        "git_commit": git_commit(),
        "scanner_file": scanner_source_path(XiaoyaScanner),
        "scan": result.to_dict(),
        "checks": checks,
        "failure_layer": "" if ok else classify_failure_layer(result, checks),
        "xiaoya_assignments": [
            item
            for item in assignments
            if item["platform"] == "小雅"
            and (
                item["title"] in {"作业-08", "实习1 分子对称性"}
                or "点阵理论" in str(item["title"])
                or item["course"] == "结构化学"
            )
        ],
        "todos": [
            item
            for item in todos
            if item["platform"] == "小雅" or item["platform"] == "长江雨课堂"
        ],
    }
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0 if ok else 1


def classify_failure_layer(result, checks: dict[str, bool]) -> str:
    if result.errors:
        return "A. 页面没打开或扫描过程失败"
    if result.candidates_count == 0:
        return "B. 页面打开但没解析"
    if not checks["db_has_zuoye_08"] or not checks["db_has_lattice"]:
        return "D. normalize 成功但 DB 没写入，或解析结果被过滤"
    if not checks["todo_has_zuoye_08"] or not checks["todo_has_lattice"]:
        return "E. DB 有但 todo 查询没返回"
    if checks["todo_has_completed_symmetry"] or checks["todo_has_fake_structure_summary"]:
        return "C. normalize/filter 规则错误"
    return "F. API 有但前端没展示"


__all__ = ["app", "main"]


if __name__ == "__main__":
    raise SystemExit(main())
