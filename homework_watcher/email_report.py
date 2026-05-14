from __future__ import annotations

import os
import smtplib
from dataclasses import dataclass
from datetime import datetime, timedelta
from email.message import EmailMessage

from .datetime_utils import human_datetime, now_local
from .models import Assignment
from .recurring_assignments import DEFAULT_RECURRING_RULES, RECURRING_PLATFORM


DEFAULT_SMTP_PORT = 587


@dataclass(frozen=True)
class EmailConfig:
    host: str
    port: int
    username: str
    password: str
    sender: str
    recipients: list[str]
    use_ssl: bool = False
    starttls: bool = True


def email_config_from_env() -> EmailConfig:
    host = os.environ.get("SMTP_HOST", "").strip()
    port = int(os.environ.get("SMTP_PORT", "").strip() or str(DEFAULT_SMTP_PORT))
    username = os.environ.get("SMTP_USERNAME", "").strip()
    password = os.environ.get("SMTP_PASSWORD", "")
    sender = os.environ.get("EMAIL_FROM", username).strip()
    recipients = parse_recipients(os.environ.get("EMAIL_TO", ""))
    use_ssl = truthy(os.environ.get("SMTP_SSL", "0"))
    starttls_raw = os.environ.get("SMTP_STARTTLS", "").strip() or "1"
    starttls = truthy(starttls_raw) and not use_ssl

    missing = []
    if not host:
        missing.append("SMTP_HOST")
    if not username:
        missing.append("SMTP_USERNAME")
    if not password:
        missing.append("SMTP_PASSWORD")
    if not sender:
        missing.append("EMAIL_FROM")
    if not recipients:
        missing.append("EMAIL_TO")
    if missing:
        raise ValueError("缺少邮件配置：" + ", ".join(missing))

    return EmailConfig(
        host=host,
        port=port,
        username=username,
        password=password,
        sender=sender,
        recipients=recipients,
        use_ssl=use_ssl,
        starttls=starttls,
    )


def parse_recipients(value: str) -> list[str]:
    return [part.strip() for part in value.replace(";", ",").split(",") if part.strip()]


def truthy(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


def build_email_subject(assignments: list[Assignment], *, now: datetime | None = None) -> str:
    now = now or now_local()
    report_assignments = filter_report_assignments(assignments, now=now)
    stats = assignment_stats(report_assignments, now=now)
    return (
        f"作业日报 {now.date().isoformat()}："
        f"待办 {stats['total']}，今日 {stats['today']}，明日 {stats['tomorrow']}，逾期 {stats['overdue']}"
    )


def build_email_report(assignments: list[Assignment], *, now: datetime | None = None) -> str:
    now = now or now_local()
    pending = sorted(
        filter_report_assignments(assignments, now=now),
        key=lambda item: (item.due_at, item.id or 0),
    )
    stats = assignment_stats(pending, now=now)

    lines = [
        f"作业日报（{now.date().isoformat()}）",
        "",
        f"统计：待办 {stats['total']}，今日截止 {stats['today']}，明日截止 {stats['tomorrow']}，逾期未提交 {stats['overdue']}",
        "",
    ]
    sections = [
        ("逾期未提交", [item for item in pending if item.due_at < now]),
        ("今日截止", [item for item in pending if item.due_at.date() == now.date() and item.due_at >= now]),
        ("明日截止", [item for item in pending if item.due_at.date() == now.date() + timedelta(days=1)]),
        ("未来待办", [item for item in pending if item.due_at.date() > now.date() + timedelta(days=1)]),
    ]
    item_numbers = {id(item): number for number, item in enumerate(pending, start=1)}
    for title, items in sections:
        lines.append(f"{title}：")
        if not items:
            lines.append("  无")
        else:
            for item in items:
                lines.append(format_assignment_line(item, now=now, number=item_numbers[id(item)]))
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def assignment_stats(assignments: list[Assignment], *, now: datetime) -> dict[str, int]:
    pending = [item for item in assignments if not item.is_done]
    today = now.date()
    tomorrow = today + timedelta(days=1)
    return {
        "total": len(pending),
        "today": sum(1 for item in pending if item.due_at.date() == today and item.due_at >= now),
        "tomorrow": sum(1 for item in pending if item.due_at.date() == tomorrow),
        "overdue": sum(1 for item in pending if item.due_at < now),
    }


def format_assignment_line(assignment: Assignment, *, now: datetime, number: int | None = None) -> str:
    parts = [
        f"课程：{assignment.course or '未填写'}",
        f"作业：{assignment.title}",
        f"平台：{display_platform(assignment)}",
        f"截止日期：{human_datetime(assignment.due_at)}",
    ]
    prefix = f"{number}. " if number is not None else ""
    return f"  {prefix}" + " | ".join(parts) + f" [{relative_due_label(assignment.due_at, now=now)}]"


def relative_due_label(due_at: datetime, *, now: datetime) -> str:
    if due_at >= now:
        return "距今：" + human_duration(due_at - now)
    return "距今：已逾期" + human_duration(now - due_at)


def human_duration(delta: timedelta) -> str:
    total_minutes = max(0, int(delta.total_seconds() // 60))
    days, remainder = divmod(total_minutes, 24 * 60)
    hours, minutes = divmod(remainder, 60)
    parts = []
    if days:
        parts.append(f"{days}天")
    if hours:
        parts.append(f"{hours}小时")
    if minutes or not parts:
        parts.append(f"{minutes}分钟")
    return "".join(parts)


def filter_report_assignments(assignments: list[Assignment], *, now: datetime) -> list[Assignment]:
    return [
        item
        for item in assignments
        if not item.is_done and (not is_recurring_assignment(item) or due_this_week(item, now=now))
    ]


def is_recurring_assignment(assignment: Assignment) -> bool:
    if assignment.platform == RECURRING_PLATFORM:
        return True
    if (assignment.source_text or "").startswith("recurring:"):
        return True
    return recurring_rule_for(assignment) is not None


def display_platform(assignment: Assignment) -> str:
    if assignment.platform != RECURRING_PLATFORM:
        return assignment.platform or "未填写"
    rule = recurring_rule_for(assignment)
    return rule.platform if rule is not None else assignment.platform


def recurring_rule_for(assignment: Assignment):
    for rule in DEFAULT_RECURRING_RULES:
        if assignment.course == rule.course and assignment.title == rule.title:
            return rule
    return None


def due_this_week(assignment: Assignment, *, now: datetime) -> bool:
    start = now.date() - timedelta(days=now.weekday())
    end = start + timedelta(days=7)
    due_date = assignment.due_at.date()
    return start <= due_date < end


def send_email_report(
    assignments: list[Assignment],
    *,
    config: EmailConfig,
    now: datetime | None = None,
) -> str:
    now = now or now_local()
    subject = build_email_subject(assignments, now=now)
    body = build_email_report(assignments, now=now)
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = config.sender
    message["To"] = ", ".join(config.recipients)
    message.set_content(body)

    smtp_cls = smtplib.SMTP_SSL if config.use_ssl else smtplib.SMTP
    with smtp_cls(config.host, config.port, timeout=30) as smtp:
        if config.starttls:
            smtp.starttls()
        smtp.login(config.username, config.password)
        smtp.send_message(message)
    return subject
