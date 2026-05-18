from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import timedelta
from pathlib import Path

from .config import DEFAULT_DB_PATH, db_path, ensure_app_dirs
from .datetime_utils import human_datetime, now_local, parse_datetime
from .db import HomeworkDB
from .email_report import build_email_report, build_email_subject, email_config_from_env, send_email_report
from .notifier import Notifier
from .parser import parse_assignments
from .recurring_assignments import DEFAULT_RECURRING_HORIZON_DAYS, materialize_recurring_assignments
from .reminders import remind_new_assignment, run_due_reminders
from .statuses import assignment_is_done, platform_status_is_done, platform_status_is_unavailable
from .summary import build_daily_summary


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    if not hasattr(args, "handler"):
        parser.print_help()
        return 0
    ensure_app_dirs()
    try:
        return args.handler(args)
    except BrokenPipeError:
        return 1
    except (KeyError, RuntimeError, ValueError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hw", description="macOS 本地作业提醒系统")
    parser.add_argument("--db", default=str(db_path()), help=f"SQLite 数据库路径，默认 {DEFAULT_DB_PATH}")

    subparsers = parser.add_subparsers(dest="command")

    add_parser = subparsers.add_parser("add", help="手动添加作业")
    add_parser.add_argument("title")
    add_parser.add_argument("--course", default="")
    add_parser.add_argument("--platform", default="")
    add_parser.add_argument("--due", required=True)
    add_parser.add_argument("--no-notify", action="store_true")
    add_parser.set_defaults(handler=cmd_add)

    list_parser = subparsers.add_parser("list", help="列出作业")
    list_parser.add_argument("--all", action="store_true", help="包含已完成作业")
    list_parser.set_defaults(handler=cmd_list)

    done_parser = subparsers.add_parser("done", help="标记作业已完成")
    done_parser.add_argument("assignment_id", type=int)
    done_parser.set_defaults(handler=cmd_done)

    import_parser = subparsers.add_parser("import-text", help="从粘贴文本解析作业")
    import_parser.add_argument("--file", type=Path, help="从文本文件读取；未指定时从 stdin 读取")
    import_parser.add_argument("--no-notify", action="store_true")
    import_parser.set_defaults(handler=cmd_import_text)

    login_parser = subparsers.add_parser("login", help="打开平台页面，手动登录并保存本地浏览器登录态")
    login_parser.add_argument("platform", help="平台：changjiang-yuketang/yuketang/长江雨课堂/xiaoya/小雅")
    login_parser.set_defaults(handler=cmd_login)

    cloud_login_parser = subparsers.add_parser("cloud-login", help="在云端有界面浏览器中打开平台登录页并保持一段时间")
    cloud_login_parser.add_argument("platforms", nargs="*", default=["all"], help="默认打开全部平台登录页")
    cloud_login_parser.add_argument("--hold-minutes", type=int, default=30, help="浏览器保持打开的分钟数")
    cloud_login_parser.set_defaults(handler=cmd_cloud_login)

    scan_parser = subparsers.add_parser("scan", help="使用 Playwright 从平台页面读取作业并写入数据库")
    scan_parser.add_argument("platforms", nargs="*", default=["all"], help="默认扫描全部平台")
    scan_parser.add_argument("--headed", action="store_true", help="显示浏览器窗口，便于排查页面结构")
    scan_parser.add_argument("--json", action="store_true", help="以统一 JSON 格式输出平台适配器结果")
    scan_parser.add_argument("--no-notify", action="store_true")
    scan_parser.set_defaults(handler=cmd_scan)

    check_parser = subparsers.add_parser("check", help="检查临近截止和逾期作业，并输出每日汇总")
    check_parser.add_argument("--scan", action="store_true", help="提醒前先用 Playwright 扫描平台作业")
    check_parser.add_argument("--headed-scan", action="store_true", help="扫描时显示浏览器窗口")
    check_parser.add_argument("--no-notify", action="store_true")
    check_parser.set_defaults(handler=cmd_check)

    summary_parser = subparsers.add_parser("summary", help="输出今日、明日、逾期汇总")
    summary_parser.set_defaults(handler=cmd_summary)

    email_parser = subparsers.add_parser("email-report", help="把未完成作业日报发送到邮箱")
    email_parser.add_argument("--dry-run", action="store_true", help="只打印邮件标题和正文，不发送")
    email_parser.add_argument("--no-sync-recurring", action="store_true", help="发送前不补齐固定每周作业")
    email_parser.set_defaults(handler=cmd_email_report)

    recurring_parser = subparsers.add_parser("sync-recurring", help="补齐固定每周作业")
    recurring_parser.add_argument("--horizon-days", type=int, default=DEFAULT_RECURRING_HORIZON_DAYS)
    recurring_parser.set_defaults(handler=cmd_sync_recurring)

    return parser


def open_db(args) -> HomeworkDB:
    return HomeworkDB(Path(args.db))


def cmd_add(args) -> int:
    due_at = parse_datetime(args.due)
    db = open_db(args)
    try:
        assignment, created = db.add_assignment(
            title=args.title,
            course=args.course,
            platform=args.platform,
            due_at=due_at,
        )
        if created:
            remind_new_assignment(db, Notifier(enabled=not args.no_notify), assignment)
            print(f"已添加 #{assignment.id}：{assignment.title}，截止 {human_datetime(assignment.due_at)}")
        else:
            print(f"已存在 #{assignment.id}：{assignment.title}，截止 {human_datetime(assignment.due_at)}")
        return 0
    finally:
        db.close()


def cmd_list(args) -> int:
    db = open_db(args)
    try:
        assignments = db.list_assignments(include_done=args.all)
        print_assignments(assignments)
        return 0
    finally:
        db.close()


def cmd_done(args) -> int:
    db = open_db(args)
    try:
        assignment = db.mark_done(args.assignment_id)
        print(f"已完成 #{assignment.id}：{assignment.title}")
        return 0
    finally:
        db.close()


def cmd_import_text(args) -> int:
    text = read_import_text(args)
    parsed = parse_assignments(text)
    if not parsed:
        print("未解析到包含截止时间的作业。", file=sys.stderr)
        return 1
    db = open_db(args)
    notifier = Notifier(enabled=not args.no_notify)
    try:
        created_count = 0
        for item in parsed:
            assignment, created = db.add_assignment(
                title=item.title,
                course=item.course,
                platform=item.platform,
                due_at=item.due_at,
                source_text=item.raw_text,
            )
            if created:
                created_count += 1
                remind_new_assignment(db, notifier, assignment)
                print(f"已导入 #{assignment.id}：{assignment.title}，截止 {human_datetime(assignment.due_at)}")
            else:
                print(f"已存在 #{assignment.id}：{assignment.title}，截止 {human_datetime(assignment.due_at)}")
        print(f"导入完成：新增 {created_count} 条，识别 {len(parsed)} 条。")
        return 0
    finally:
        db.close()


def cmd_login(args) -> int:
    from .platforms import get_adapter

    adapter = get_adapter(args.platform)
    adapter.manual_login()
    print(f"{adapter.platform_name} 登录态已保存在本地浏览器配置：{adapter.user_data_dir}")
    return 0


def cmd_cloud_login(args) -> int:
    from .platforms import iter_adapters
    from .platforms.base import PlaywrightUnavailableError, load_playwright, format_playwright_error

    adapters = list(iter_adapters(args.platforms))
    hold_seconds = max(1, args.hold_minutes) * 60
    sync_playwright, playwright_error = load_playwright()
    contexts = []
    with sync_playwright() as playwright:
        try:
            for adapter in adapters:
                context = adapter._launch_context(playwright, headless=False)
                contexts.append((adapter, context))
                page = context.pages[0] if context.pages else context.new_page()
                page.goto(adapter.url, wait_until="domcontentloaded", timeout=adapter.timeout_ms)
                print(f"已打开 {adapter.platform_name} 登录页：{adapter.url}", flush=True)
            print("请在远程浏览器中手动登录。程序不会读取、保存或提交你的密码。", flush=True)
            print(f"浏览器会保持打开 {args.hold_minutes} 分钟；时间结束后会关闭并保存云端登录态。", flush=True)
            time.sleep(hold_seconds)
        except playwright_error as exc:
            raise PlaywrightUnavailableError(format_playwright_error(exc)) from exc
        finally:
            for _, context in contexts:
                context.close()
    for adapter, _ in contexts:
        print(f"{adapter.platform_name} 云端登录态已保存：{adapter.user_data_dir}")
    return 0


def cmd_scan(args) -> int:
    db = open_db(args)
    notifier = Notifier(enabled=not args.no_notify)
    try:
        records, had_error = scan_platforms(
            db,
            platforms=args.platforms,
            headed=args.headed,
            notifier=notifier,
            progress=print_progress,
        )
        if args.json:
            print(json.dumps([record_to_json(record) for record in records], ensure_ascii=False, indent=2))
        else:
            print_scan_records(records)
        return 2 if had_error else 0
    finally:
        db.close()


def cmd_check(args) -> int:
    db = open_db(args)
    notifier = Notifier(enabled=not args.no_notify)
    try:
        if args.scan:
            records, had_error = scan_platforms(
                db,
                platforms=["all"],
                headed=args.headed_scan,
                notifier=notifier,
                progress=print_progress,
            )
            if records:
                print(f"平台扫描完成：发现 {len(records)} 条，新增 {sum(1 for record in records if record['created'])} 条。")
            if had_error:
                print("平台扫描存在错误，已继续执行本地提醒检查。", file=sys.stderr)

        recurring_created = materialize_recurring_assignments(db)
        if recurring_created:
            print(f"固定作业补齐：新增 {len(recurring_created)} 条。")

        events = run_due_reminders(db, notifier)
        if events:
            for event in events:
                print(f"已提醒：#{event.assignment.id} {event.assignment.title} ({event.title})")
        else:
            print("没有新的提醒。")
        print()
        print(build_daily_summary(db.list_assignments(include_done=False)))
        return 0
    finally:
        db.close()


def cmd_summary(args) -> int:
    db = open_db(args)
    try:
        print(build_daily_summary(db.list_assignments(include_done=False)))
        return 0
    finally:
        db.close()


def cmd_email_report(args) -> int:
    db = open_db(args)
    try:
        now = now_local()
        if not args.no_sync_recurring:
            materialize_recurring_assignments(db, now=now, horizon_days=days_until_end_of_week(now))
        assignments = db.list_assignments(include_done=False)
        if args.dry_run:
            print(f"Subject: {build_email_subject(assignments, now=now)}")
            print()
            print(build_email_report(assignments, now=now), end="")
            return 0
        subject = send_email_report(assignments, config=email_config_from_env(), now=now)
        print(f"已发送作业日报：{subject}")
        return 0
    finally:
        db.close()


def cmd_sync_recurring(args) -> int:
    db = open_db(args)
    try:
        generated = materialize_recurring_assignments(db, horizon_days=args.horizon_days)
        if generated:
            for assignment in generated:
                print(f"已添加固定作业 #{assignment.id}：{assignment.title}，截止 {human_datetime(assignment.due_at)}")
        print(f"固定作业补齐完成：新增 {len(generated)} 条。")
        return 0
    finally:
        db.close()


def days_until_end_of_week(now) -> int:
    days_until_sunday = 6 - now.weekday()
    end_of_sunday = now.replace(hour=23, minute=59, second=59, microsecond=0) + timedelta(days=days_until_sunday)
    return max(0, (end_of_sunday - now).days + 1)


def read_import_text(args) -> str:
    if args.file:
        return args.file.read_text(encoding="utf-8")
    if sys.stdin.isatty():
        print("请粘贴作业文本，结束后按 Ctrl-D：", file=sys.stderr)
    return sys.stdin.read()


def print_assignments(assignments) -> None:
    if not assignments:
        print("没有作业。")
        return
    now = now_local()
    print("ID  状态    截止时间          平台          课程          平台状态      标题")
    for item in assignments:
        status = status_for(item, now)
        platform = truncate(item.platform, 12)
        course = truncate(item.course, 12)
        platform_status = truncate(item.status, 10)
        print(
            f"{item.id:<3} {status:<6} {human_datetime(item.due_at):<16} "
            f"{platform:<12} {course:<12} {platform_status:<10} {item.title}"
        )


def status_for(item, now) -> str:
    if assignment_is_done(item):
        return "已完成"
    if platform_status_is_unavailable(item.status):
        return "不可完成"
    if item.due_at < now:
        return "逾期"
    if item.due_at.date() == now.date():
        return "今日"
    return "待办"


def truncate(value: str, length: int) -> str:
    return value if len(value) <= length else value[: length - 1] + "…"


def scan_platforms(
    db: HomeworkDB,
    *,
    platforms: list[str],
    headed: bool,
    notifier: Notifier,
    progress=None,
) -> tuple[list[dict], bool]:
    from .platforms import iter_adapters
    from .platforms.base import LoginRequiredError, PageStructureChangedError, PlaywrightUnavailableError

    records: list[dict] = []
    had_error = False
    for adapter in iter_adapters(platforms):
        try:
            if progress is not None:
                progress(f"开始扫描 {adapter.platform_name}")
            items = adapter.fetch_assignments(headless=not headed, progress=progress)
        except LoginRequiredError as exc:
            had_error = True
            message = f"{adapter.platform_name} 登录状态已失效，请运行：hw login {adapter.slug}"
            notifier.notify("需要重新登录作业平台", message, subtitle=adapter.platform_name)
            print(f"登录失效：{exc}", file=sys.stderr)
            continue
        except PageStructureChangedError as exc:
            had_error = True
            print(f"页面结构错误：{exc}", file=sys.stderr)
            continue
        except PlaywrightUnavailableError as exc:
            had_error = True
            print(f"Playwright 错误：{exc}", file=sys.stderr)
            continue

        if progress is not None:
            progress(f"{adapter.platform_name}：写入数据库 {len(items)} 条")
        for item in items:
            assignment, created = db.add_assignment(
                title=item.title,
                course=item.course,
                platform=item.platform,
                due_at=item.due_at,
                status=item.status,
                url=item.url,
                source_text=item.url,
            )
            if assignment.id is not None and platform_status_is_done(item.status):
                assignment = db.mark_done(assignment.id)
            if created and not platform_status_is_unavailable(item.status):
                remind_new_assignment(db, notifier, assignment)
            records.append({"adapter": adapter, "item": item, "assignment": assignment, "created": created})
    return records, had_error


def print_progress(message: str) -> None:
    print(f"[scan] {message}", file=sys.stderr, flush=True)


def record_to_json(record: dict) -> dict:
    item = record["item"].to_dict()
    item["id"] = record["assignment"].id
    item["created"] = record["created"]
    return item


def print_scan_records(records: list[dict]) -> None:
    if not records:
        print("没有扫描到作业。")
        return
    for record in records:
        assignment = record["assignment"]
        item = record["item"]
        action = "新增" if record["created"] else "已存在"
        url = f" {item.url}" if item.url else ""
        print(
            f"{action} #{assignment.id} [{item.platform}] {item.title} "
            f"截止 {human_datetime(item.due_at)} 状态 {item.status}{url}"
        )
