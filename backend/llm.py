"""
LLM 调用层：支持用户自定义的 OpenAI 兼容 / Anthropic 兼容供应商。
"""
import logging
from typing import Any, Dict, Optional

import httpx

logger = logging.getLogger(__name__)

TIMEOUT = httpx.Timeout(600, connect=30)
MAX_TOKENS = 8000


def find_provider(settings: Dict[str, Any], provider_id: str) -> Dict[str, Any]:
    for provider in settings["llm"]["providers"]:
        if provider.get("id") == provider_id:
            return provider
    raise RuntimeError("未选择 LLM 供应商，或所选供应商已被删除，请在设置中配置")


def _http_client(provider: Dict[str, Any], proxy: Optional[str]) -> httpx.Client:
    use_proxy = provider.get("use_proxy", True) and proxy
    # trust_env=False：不读取系统代理环境变量，是否走代理完全由设置决定
    return httpx.Client(proxy=proxy if use_proxy else None, timeout=TIMEOUT, trust_env=False)


def complete(
    provider: Dict[str, Any],
    system: str,
    user: str,
    *,
    model: str = "",
    temperature: float = 0.3,
    proxy: Optional[str] = None,
) -> str:
    model = model or provider.get("model") or ""
    if not model:
        raise RuntimeError(f"供应商「{provider.get('name')}」未填写模型名")
    protocol = provider.get("protocol", "openai")
    logger.info("调用 LLM: %s / %s", provider.get("name"), model)

    with _http_client(provider, proxy) as http_client:
        if protocol == "anthropic":
            import anthropic

            client = anthropic.Anthropic(
                api_key=provider.get("api_key") or "",
                base_url=provider.get("base_url") or None,
                http_client=http_client,
            )
            message = client.messages.create(
                model=model,
                max_tokens=MAX_TOKENS,
                temperature=temperature,
                system=system,
                messages=[{"role": "user", "content": user}],
            )
            text = "".join(block.text for block in message.content if getattr(block, "type", "") == "text")
        elif protocol == "openai":
            from openai import OpenAI

            client = OpenAI(
                api_key=provider.get("api_key") or "sk-placeholder",
                base_url=provider.get("base_url") or None,
                http_client=http_client,
            )
            completion = client.chat.completions.create(
                model=model,
                temperature=temperature,
                max_tokens=MAX_TOKENS,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            )
            text = completion.choices[0].message.content or ""
        else:
            raise RuntimeError(f"不支持的协议类型: {protocol}")

    if not text.strip():
        raise RuntimeError("LLM 返回为空")
    return text.strip()
