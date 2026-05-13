from __future__ import annotations

import os
import smtplib
from dataclasses import dataclass
from datetime import datetime, timedelta
from email.message import EmailMessage

from .datetime_utils import human_datetime, now_local
from .models import Assignment


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
    stats = assignment_stats(assignments, now=now)
    return (
        f"作业日报 {now.date().isoformat()}："
        f"待办 {stats['total']}，今日 {stats['today']}，明日 {stats['tomorrow']}，逾期 {stats['overdue']}"
    )


def build_email_report(assignments: list[Assignment], *, now: datetime | None = None) -> str:
    now = now or now_local()
    pending = sorted([item for item in assignments if not item.is_done], key=lambda item: (item.due_at, item.id or 0))
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
    for title, items in sections:
        lines.append(f"{title}：")
        if not items:
            lines.append("  无")
        else:
            for item in items:
                lines.append(format_assignment_line(item, now=now))
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


def format_assignment_line(assignment: Assignment, *, now: datetime) -> str:
    parts = [f"#{assignment.id}" if assignment.id is not None else "#?", human_datetime(assignment.due_at)]
    meta = " / ".join(part for part in [assignment.course, assignment.platform] if part)
    if meta:
        parts.append(f"[{meta}]")
    parts.append(assignment.title)
    if assignment.status:
        parts.append(f"状态：{assignment.status}")
    if assignment.url:
        parts.append(f"链接：{assignment.url}")
    if assignment.due_at < now:
        parts.append("已逾期")
    return "  " + " ".join(parts)


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
