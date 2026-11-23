"""运行时配置：全部来自环境变量，不在代码中内嵌任何连接串或密钥。"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date
from functools import lru_cache

DEFAULT_ADJUST_MAX_CENTS = 5_000_000


class ConfigError(RuntimeError):
    """配置缺失或不合法时抛出。"""


def _parse_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = int(raw.strip())
    except ValueError as exc:  # pragma: no cover - 仅在配置错误时触发
        raise ConfigError(f"{name} 必须是整数，实际收到 {raw!r}") from exc
    if value <= 0:
        raise ConfigError(f"{name} 必须为正整数，实际收到 {value}")
    return value


@dataclass(frozen=True)
class Settings:
    database_url: str
    adjust_max_cents: int
    current_period: date

    @property
    def current_period_label(self) -> str:
        return self.current_period.strftime("%Y-%m")


def _parse_period(raw: str | None) -> date:
    if not raw:
        today = date.today()
        return date(today.year, today.month, 1)
    try:
        year_s, month_s = raw.split("-", 1)
        return date(int(year_s), int(month_s), 1)
    except (ValueError, AttributeError) as exc:
        raise ConfigError(f"COMM_CURRENT_PERIOD 需为 YYYY-MM，实际收到 {raw!r}") from exc


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    url = os.getenv("DATABASE_URL")
    if not url:
        raise ConfigError("缺少环境变量 DATABASE_URL")
    return Settings(
        database_url=url,
        adjust_max_cents=_parse_int("COMM_ADJUST_MAX_CENTS", DEFAULT_ADJUST_MAX_CENTS),
        current_period=_parse_period(os.getenv("COMM_CURRENT_PERIOD")),
    )


def reset_settings_cache() -> None:
    """测试与 demo 脚本在改动环境变量后调用。"""
    get_settings.cache_clear()
