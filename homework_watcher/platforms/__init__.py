"""Platform adapters for Playwright-based homework discovery."""

from __future__ import annotations

from .changjiang_yuketang import ChangjiangYuketangAdapter
from .xiaoya import XiaoyaAdapter


ADAPTER_CLASSES = {
    "changjiang-yuketang": ChangjiangYuketangAdapter,
    "yuketang": ChangjiangYuketangAdapter,
    "长江雨课堂": ChangjiangYuketangAdapter,
    "xiaoya": XiaoyaAdapter,
    "小雅": XiaoyaAdapter,
}


def platform_choices() -> list[str]:
    return sorted(ADAPTER_CLASSES)


def canonical_slugs() -> list[str]:
    seen: set[str] = set()
    slugs: list[str] = []
    for adapter_class in ADAPTER_CLASSES.values():
        if adapter_class.slug not in seen:
            slugs.append(adapter_class.slug)
            seen.add(adapter_class.slug)
    return slugs


def get_adapter(name: str):
    try:
        return ADAPTER_CLASSES[name]()
    except KeyError as exc:
        choices = ", ".join(canonical_slugs())
        raise ValueError(f"未知平台：{name}。可用平台：{choices}") from exc


def iter_adapters(names: list[str] | None = None):
    selected = names or ["all"]
    if selected == ["all"] or "all" in selected:
        for slug in canonical_slugs():
            yield get_adapter(slug)
        return
    yielded: set[str] = set()
    for name in selected:
        adapter = get_adapter(name)
        if adapter.slug in yielded:
            continue
        yielded.add(adapter.slug)
        yield adapter
