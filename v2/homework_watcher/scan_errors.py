from __future__ import annotations

from dataclasses import dataclass
from typing import Any


PLATFORM_LABELS = {
    "xiaoya": "小雅",
    "changjiang-yuketang": "长江雨课堂",
    "fake": "测试平台",
}

NEEDS_ACTION_STATUSES = {"failed", "needs_action"}


@dataclass(frozen=True)
class ScanErrorAdvice:
    code: str
    title: str
    summary: str
    actions: tuple[str, ...]
    technical_detail: str = ""
    platform: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "code": self.code,
            "title": self.title,
            "summary": self.summary,
            "actions": list(self.actions),
            "technical_detail": self.technical_detail,
            "platform": self.platform,
        }

    def to_text(self) -> str:
        parts = [self.title, self.summary]
        if self.actions:
            parts.append("处理方法：" + "；".join(self.actions))
        if self.technical_detail:
            parts.append(f"技术细节：{self.technical_detail}")
        return "\n".join(part for part in parts if part)


def describe_scan_exception(exc: BaseException, *, platform_key: str = "") -> ScanErrorAdvice:
    detail = f"{type(exc).__name__}: {exc}"
    return describe_scan_error_text(detail, platform_key=platform_key)


def describe_scan_error_text(message: str, *, platform_key: str = "") -> ScanErrorAdvice:
    detail = compact_detail(message)
    lowered = detail.casefold()
    platform = platform_key or infer_platform_key(detail)
    platform_label = platform_display_name(platform)

    if _contains_any(
        lowered,
        (
            "登录态可能失效",
            "需要重新登录",
            "login required",
            "auth required",
            "passport",
            "sso",
        ),
    ) or ("登录" in detail and _contains_any(detail, ("密码", "验证码", "扫码"))):
        return ScanErrorAdvice(
            code="auth_required",
            title=f"{platform_label or '平台'}需要重新登录",
            summary="扫描时打开的是平台登录页，说明服务器保存的浏览器登录态已过期，或上次授权没有完成。",
            actions=(
                "回到首页的“平台登录”区域，打开对应平台",
                "在远程浏览器里完成扫码或账号登录，然后点击“我已完成登录”",
                "再重新点击“扫描课程”或“扫描任务”",
            ),
            technical_detail=detail,
            platform=platform,
        )

    if _contains_any(detail, ("没有已保存课程", "请先扫描课程")):
        return ScanErrorAdvice(
            code="xiaoya_courses_missing",
            title="小雅还没有课程列表",
            summary="扫描任务需要先知道小雅有哪些课程；当前账号还没有保存过课程列表。",
            actions=(
                "先点击首页的“扫描课程”",
                "如果扫描课程仍为空，重新完成“小雅登录”后再扫描课程",
                "课程保存成功后再点击“扫描任务”",
            ),
            technical_detail=detail,
            platform=platform or "xiaoya",
        )

    if _contains_any(detail, ("未发现可保存课程", "没有发现可保存课程", "未发现可扫描课程", "no-courses")):
        return ScanErrorAdvice(
            code="xiaoya_courses_not_found",
            title="小雅课程扫描没有发现课程",
            summary="程序打开了小雅课程页，但没有读取到可保存的课程；常见原因是小雅登录态失效、账号课程页为空，或平台页面暂时没有加载出来。",
            actions=(
                "先在“平台登录”里重新登录小雅，并确认课程页能看到课程",
                "重新点击“扫描课程”",
                "如果仍为空，稍后重试并查看最近扫描日志里的调试页面路径",
            ),
            technical_detail=detail,
            platform=platform or "xiaoya",
        )

    if _contains_any(
        lowered,
        (
            "executable doesn't exist",
            "playwright install",
            "browser executable",
            "failed to launch",
            "chromium",
        ),
    ):
        return ScanErrorAdvice(
            code="browser_runtime_missing",
            title="服务器缺少浏览器运行环境",
            summary="扫描需要服务器上的 Playwright Chromium 浏览器；当前环境没有安装完整或无法启动。",
            actions=(
                "在服务器执行 python -m playwright install chromium",
                "如果是重新部署，重新运行安装脚本后重启 homework-watcher 服务",
                "完成后再重新扫描",
            ),
            technical_detail=detail,
            platform=platform,
        )

    if _contains_any(
        lowered,
        (
            "processsingleton",
            "user data directory is already in use",
            "profile is already in use",
            "singletonlock",
            "target page, context or browser has been closed",
            "browser has been closed",
        ),
    ):
        return ScanErrorAdvice(
            code="browser_profile_busy",
            title="平台浏览器会话被占用或已关闭",
            summary="同一个平台账号的远程登录窗口或扫描浏览器还在占用登录态目录，或者扫描过程中浏览器被关闭。",
            actions=(
                "先结束正在进行的远程登录或等待当前扫描结束",
                "必要时点击“强制结束”，等待一分钟后重试",
                "如果刚完成平台登录，请点击“我已完成登录”后再扫描",
            ),
            technical_detail=detail,
            platform=platform,
        )

    if _contains_any(
        lowered,
        (
            "net::err",
            "err_name_not_resolved",
            "err_connection",
            "err_timed_out",
            "connection reset",
            "connection refused",
            "name or service not known",
        ),
    ):
        return ScanErrorAdvice(
            code="network_unreachable",
            title=f"{platform_label or '平台'}暂时无法连接",
            summary="服务器访问平台页面失败，通常是网络、DNS、校园网或平台临时故障导致。",
            actions=(
                "稍后重新扫描",
                "确认服务器能访问对应教学平台",
                "如果平台只允许校园网访问，请先处理校园网或 VPN 连接",
            ),
            technical_detail=detail,
            platform=platform,
        )

    if _contains_any(lowered, ("timeouterror", "timeout", "timed out", "超时")):
        return ScanErrorAdvice(
            code="page_timeout",
            title=f"{platform_label or '平台'}页面加载超时",
            summary="平台页面在限定时间内没有加载到作业或课程内容；可能是平台响应慢、登录态过期，或课程页面结构变化。",
            actions=(
                "先重试一次扫描",
                "如果连续失败，重新完成对应平台登录",
                "课程有变化时先扫描课程，再扫描任务",
            ),
            technical_detail=detail,
            platform=platform,
        )

    if _contains_any(
        detail,
        (
            "缺少平台配置",
            "未启用",
            "platform config not found",
            "has no scanner",
            "暂不支持课程缓存扫描",
        ),
    ):
        return ScanErrorAdvice(
            code="platform_config",
            title=f"{platform_label or '平台'}配置需要调整",
            summary="当前扫描请求的平台没有启用、缺少配置，或还没有对应的扫描器。",
            actions=(
                "检查 v2/config/platforms.yaml 中的平台配置是否存在并 enabled: true",
                "确认点击的是该平台支持的扫描入口",
                "保存配置后重启服务再扫描",
            ),
            technical_detail=detail,
            platform=platform,
        )

    if _contains_any(
        lowered,
        (
            "database is locked",
            "readonly database",
            "permission denied",
            "no such file or directory",
            "disk i/o error",
        ),
    ):
        return ScanErrorAdvice(
            code="storage_unavailable",
            title="服务器数据文件不可写或被占用",
            summary="扫描结果需要写入本地数据库或调试目录，但当前文件被占用、路径不存在，或服务没有写入权限。",
            actions=(
                "稍后重试，避开正在运行的扫描",
                "检查 DATABASE_URL、日志目录和调试目录是否存在且可写",
                "修复权限后重启服务",
            ),
            technical_detail=detail,
            platform=platform,
        )

    if _contains_any(detail, ("没有返回可解析结果", "服务器扫描命令失败")):
        return ScanErrorAdvice(
            code="scan_process_failed",
            title="扫描进程异常退出",
            summary="网页已启动扫描命令，但扫描进程没有返回完整结果。",
            actions=(
                "先重新扫描一次",
                "如果仍失败，点击“查看最近扫描日志”确认具体平台和错误",
                "按日志提示重新登录平台或重启服务",
            ),
            technical_detail=detail,
            platform=platform,
        )

    return ScanErrorAdvice(
        code="unknown_scan_error",
        title=f"{platform_label + '扫描失败' if platform_label else '扫描失败'}",
        summary="扫描过程中出现未识别错误，程序已经保留技术细节用于排查。",
        actions=(
            "先点击“查看最近扫描日志”确认错误发生在哪个平台",
            "按日志中的平台重新登录后重试",
            "如果仍失败，把技术细节发给维护者",
        ),
        technical_detail=detail,
        platform=platform,
    )


def collect_scan_error_advices(
    result: dict[str, Any] | None,
    *,
    fallback: str = "",
) -> list[ScanErrorAdvice]:
    advices: list[ScanErrorAdvice] = []
    if isinstance(result, dict):
        for raw in result.get("error_details") or []:
            advice = scan_error_advice_from_dict(raw)
            if advice is not None:
                append_unique_advice(advices, advice)

        for raw in result.get("errors") or []:
            if raw:
                append_unique_advice(advices, describe_scan_error_text(str(raw)))

        summaries = result.get("platform_summaries") or {}
        if isinstance(summaries, dict):
            for platform, summary in summaries.items():
                if not isinstance(summary, dict):
                    continue
                status = str(summary.get("status") or "").strip()
                message = str(summary.get("message") or "").strip()
                if status in NEEDS_ACTION_STATUSES and message:
                    append_unique_advice(
                        advices,
                        describe_scan_error_text(message, platform_key=str(platform)),
                    )

    if not advices and fallback:
        append_unique_advice(advices, describe_scan_error_text(fallback))
    return advices


def format_scan_failure(result: dict[str, Any] | None = None, *, fallback: str = "") -> str:
    advices = collect_scan_error_advices(result, fallback=fallback)
    if not advices:
        return describe_scan_error_text(fallback or "扫描失败").to_text()
    if len(advices) == 1:
        return advices[0].to_text()

    actions: list[str] = []
    for advice in advices:
        for action in advice.actions:
            if action not in actions:
                actions.append(action)

    detail = " | ".join(advice.technical_detail for advice in advices if advice.technical_detail)
    combined = ScanErrorAdvice(
        code="multiple_scan_errors",
        title=f"扫描失败：有 {len(advices)} 个问题需要处理",
        summary="；".join(f"{advice.title}：{advice.summary}" for advice in advices[:3]),
        actions=tuple(actions[:5]),
        technical_detail=compact_detail(detail),
    )
    return combined.to_text()


def scan_error_advice_from_dict(raw: object) -> ScanErrorAdvice | None:
    if not isinstance(raw, dict):
        return None
    actions = raw.get("actions") or ()
    if isinstance(actions, str):
        action_values = (actions,)
    else:
        action_values = tuple(str(item) for item in actions if str(item).strip())
    return ScanErrorAdvice(
        code=str(raw.get("code") or "unknown_scan_error"),
        title=str(raw.get("title") or "扫描失败"),
        summary=str(raw.get("summary") or "扫描过程中出现错误。"),
        actions=action_values,
        technical_detail=str(raw.get("technical_detail") or ""),
        platform=str(raw.get("platform") or ""),
    )


def append_unique_advice(advices: list[ScanErrorAdvice], advice: ScanErrorAdvice) -> None:
    identity = (advice.code, advice.platform, advice.summary)
    if any((item.code, item.platform, item.summary) == identity for item in advices):
        return
    advices.append(advice)


def platform_display_name(platform_key: str) -> str:
    if not platform_key:
        return ""
    return PLATFORM_LABELS.get(platform_key, platform_key)


def infer_platform_key(message: str) -> str:
    lowered = message.casefold()
    if "小雅" in message or "xiaoya" in lowered or "ai-augmented" in lowered:
        return "xiaoya"
    if "长江雨课堂" in message or "雨课堂" in message or "yuketang" in lowered:
        return "changjiang-yuketang"
    return ""


def compact_detail(message: str, *, limit: int = 700) -> str:
    detail = " ".join(str(message or "").split())
    if len(detail) <= limit:
        return detail
    return detail[:limit].rstrip() + "..."


def _contains_any(value: str, needles: tuple[str, ...]) -> bool:
    return any(needle.casefold() in value.casefold() for needle in needles)
