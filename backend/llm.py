"""
LLM 调用层：支持用户自定义的 OpenAI 兼容 / Anthropic 兼容 / Google Gemini 原生（generateContent）供应商。
"""
import base64
import logging
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)

TIMEOUT = httpx.Timeout(600, connect=30)
MAX_TOKENS = 8000
# Gemini 的思考 token 也算在 maxOutputTokens 里：实测 gemini-3.8-flash 清洗一篇 4 分钟的采访思考了 8600 token，
# 8000 的上限只剩 360 token 正文，输出被截断
GEMINI_MAX_TOKENS = 32768
TRUNCATED = "LLM 输出达到长度上限被截断，请检查模型设置"
# 思考强度：""=不传参数（模型默认），off/low/medium/high 按协议换成各家的参数
EFFORTS = ("off", "low", "medium", "high")
# Gemini 2.x 用 thinkingBudget（token 数），3.x 起用 thinkingLevel
GEMINI_BUDGETS = {"off": 0, "low": 1024, "medium": 8192, "high": 24576}
GEMINI_LEVELS = {"off": "minimal", "low": "low", "medium": "medium", "high": "high"}
# OpenAI 兼容：reasoning_effort，none 表示不思考
OPENAI_EFFORTS = {"off": "none", "low": "low", "medium": "medium", "high": "high"}


def find_provider(settings: Dict[str, Any], provider_id: str) -> Dict[str, Any]:
    for provider in settings["llm"]["providers"]:
        if provider.get("id") == provider_id:
            return provider
    raise RuntimeError("未选择 LLM 供应商，或所选供应商已被删除，请在设置中配置")


def _http_client(provider: Dict[str, Any], proxy: Optional[str]) -> httpx.Client:
    use_proxy = provider.get("use_proxy", True) and proxy
    # trust_env=False：不读取系统代理环境变量，是否走代理完全由设置决定
    return httpx.Client(proxy=proxy if use_proxy else None, timeout=TIMEOUT, trust_env=False)


def _gemini_complete(http_client: httpx.Client, provider: Dict[str, Any], model: str, system: str, user: str,
                     temperature: float, images: Optional[List[bytes]] = None, effort: str = "") -> str:
    """Google 原生接口：POST {base}/v1beta/models/{model}:generateContent，密钥放在 x-goog-api-key。"""
    base = (provider.get("base_url") or "https://generativelanguage.googleapis.com").rstrip("/")
    # 兼容用户把 /v1beta 或完整路径填进 Base URL
    base = base.split("/v1beta")[0].rstrip("/")
    model = model.removeprefix("models/")
    generation_config: Dict[str, Any] = {"temperature": temperature, "maxOutputTokens": GEMINI_MAX_TOKENS}
    if effort:
        generation_config["thinkingConfig"] = (
            {"thinkingBudget": GEMINI_BUDGETS[effort]} if model.startswith("gemini-2")
            else {"thinkingLevel": GEMINI_LEVELS[effort]})
    response = http_client.post(
        f"{base}/v1beta/models/{model}:generateContent",
        headers={"x-goog-api-key": provider.get("api_key") or "", "Content-Type": "application/json"},
        json={
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}] + [
                {"inlineData": {"mimeType": "image/jpeg", "data": base64.b64encode(image).decode()}} for image in images or []]}],
            "generationConfig": generation_config,
        },
    )
    if response.status_code != 200:
        raise RuntimeError(f"Error code: {response.status_code} - {response.text[:300]}")
    data = response.json()
    candidates = data.get("candidates") or []
    if not candidates:
        reason = (data.get("promptFeedback") or {}).get("blockReason")
        raise RuntimeError(f"Gemini 没有返回内容" + (f"（{reason}）" if reason else ""))
    if candidates[0].get("finishReason") == "MAX_TOKENS":
        raise RuntimeError(TRUNCATED)
    parts = (candidates[0].get("content") or {}).get("parts") or []
    # thought=true 是思考摘要，不算正文
    return "".join(part.get("text", "") for part in parts if not part.get("thought"))


def complete(
    provider: Dict[str, Any],
    system: str,
    user: str,
    *,
    model: str = "",
    temperature: float = 0.3,
    proxy: Optional[str] = None,
    images: Optional[List[bytes]] = None,
    effort: str = "",
) -> str:
    """images：附在用户消息后的 JPEG 图片（需要模型支持图片输入）。effort：思考强度，见 EFFORTS。"""
    images = images or []
    effort = effort if effort in EFFORTS else ""
    model = model or provider.get("model") or ""
    if not model:
        raise RuntimeError(f"供应商「{provider.get('name')}」未填写模型名")
    protocol = provider.get("protocol", "openai")
    logger.info("调用 LLM: %s / %s%s", provider.get("name"), model, f"（思考 {effort}）" if effort else "")

    with _http_client(provider, proxy) as http_client:
        if protocol == "anthropic":
            import anthropic

            client = anthropic.Anthropic(
                api_key=provider.get("api_key") or "",
                base_url=provider.get("base_url") or None,
                http_client=http_client,
            )
            kwargs: Dict[str, Any] = {"max_tokens": MAX_TOKENS, "temperature": temperature}
            if effort == "off":
                kwargs["thinking"] = {"type": "disabled"}
            elif effort:
                # 开思考时不能自定义 temperature；思考 token 也算在 max_tokens 里
                kwargs = {"max_tokens": 16000, "thinking": {"type": "adaptive"},
                          "extra_body": {"output_config": {"effort": effort}}}
            message = client.messages.create(
                model=model,
                **kwargs,
                system=system,
                messages=[{"role": "user", "content": [{"type": "text", "text": user}] + [
                    {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                                 "data": base64.b64encode(image).decode()}} for image in images]}],
            )
            if message.stop_reason == "max_tokens":
                raise RuntimeError(TRUNCATED)
            text = "".join(block.text for block in message.content if getattr(block, "type", "") == "text")
        elif protocol == "gemini":
            text = _gemini_complete(http_client, provider, model, system, user, temperature, images, effort)
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
                **({"reasoning_effort": OPENAI_EFFORTS[effort]} if effort else {}),
                max_tokens=MAX_TOKENS,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": [{"type": "text", "text": user}] + [
                        {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(image).decode()}}
                        for image in images] if images else user},
                ],
            )
            if completion.choices[0].finish_reason == "length":
                raise RuntimeError(TRUNCATED)
            text = completion.choices[0].message.content or ""
        else:
            raise RuntimeError(f"不支持的协议类型: {protocol}")

    if not text.strip():
        raise RuntimeError("LLM 返回为空")
    return text.strip()
