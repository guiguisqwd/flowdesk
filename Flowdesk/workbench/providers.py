"""Explicit design-time AI selection; credentials stay on the server.

Availability means configured, not that authentication, quota, or model access
has been verified. The default remains the local Codex CLI.
"""
from __future__ import annotations

import math
import os
import re

from .codex import CodexError


def provider_name() -> str:
    return os.environ.get("WORKBENCH_AI_PROVIDER", "codex").strip().lower()


def openai_config() -> tuple[str, str, float]:
    key = os.environ.get("OPENAI_API_KEY", "").strip()
    model = os.environ.get("WORKBENCH_OPENAI_MODEL", "").strip()
    if not key or not model:
        raise CodexError("OpenAI 尚未配置：请在服务器设置 OPENAI_API_KEY 和 WORKBENCH_OPENAI_MODEL。")
    if "\r" in key or "\n" in key:
        raise CodexError("OpenAI API key 配置格式无效。")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,199}", model):
        raise CodexError("WORKBENCH_OPENAI_MODEL 配置格式无效。")
    try:
        timeout = float(os.environ.get("WORKBENCH_OPENAI_TIMEOUT", "600"))
    except ValueError:
        raise CodexError("WORKBENCH_OPENAI_TIMEOUT 必须为 0 到 600 秒之间的正数。") from None
    if not math.isfinite(timeout) or not 0 < timeout <= 600:
        raise CodexError("WORKBENCH_OPENAI_TIMEOUT 必须为 0 到 600 秒之间的正数。")
    return key, model, timeout


def provider_status(codex_path: str | None = None) -> dict:
    provider = provider_name()
    if provider == "codex":
        available = bool(codex_path)
        message = "已检测到 Codex CLI；登录与模型可用性以实际调用为准。" if available else "未找到 Codex CLI；请在本机安装并登录。"
    elif provider == "openai":
        try:
            openai_config()
            available, message = True, "OpenAI 服务端配置已就绪；凭据与模型尚需实际调用验证。"
        except CodexError as error:
            available, message = False, str(error)
    elif provider == "disabled":
        available, message = False, "AI 功能已关闭；仍可编辑、保存和导出设计。"
    else:
        # Do not echo arbitrary environment values into public diagnostics.
        provider = "invalid"
        available, message = False, "WORKBENCH_AI_PROVIDER 必须是 codex、openai 或 disabled。"
    return {"ai_provider": provider, "ai_available": available, "ai_message": message}
