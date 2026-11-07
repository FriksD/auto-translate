"""
直播间是否在播。

不能只看 streamlink 拉不拉得到流：实测 B 站 IEM 北京副舞台（live.bilibili.com/34）未开播（live_status=0）时，
streamlink 仍然给出一路流，录下来是轮播/占位画面。所以 B 站用房间接口判断，只有 live_status == 1 才算开播；
其他平台（YouTube / Twitch / Kick）用 `streamlink --json`，没有可播放的流就是未开播。

probe() 返回 True（在播）/ False（未开播）/ None（查不到，比如网络出错）。
"""
import json
import logging
import re
import subprocess
import sys
from typing import Any, Dict, Optional

import httpx

logger = logging.getLogger(__name__)

BILIBILI_ROOM = re.compile(r"live\.bilibili\.com/(?:h5/|blanc/)?(\d+)", re.I)
BILIBILI_API = "https://api.live.bilibili.com/room/v1/Room/get_info"
# 国内平台默认不走代理（省代理流量，直连也更快）
DOMESTIC_HOSTS = ("bilibili.com", "huya.com", "douyu.com", "douyin.com")
PROBE_TIMEOUT = 40
STREAMLINK_OFFLINE = ("no playable streams", "no streams found", "offline", "not live", "not currently live")


def platform(url: str) -> str:
    url = (url or "").lower()
    for name, host in (("bilibili", "bilibili.com"), ("youtube", "youtube.com"), ("youtube", "youtu.be"),
                       ("twitch", "twitch.tv"), ("kick", "kick.com"), ("huya", "huya.com"),
                       ("douyu", "douyu.com"), ("douyin", "douyin.com")):
        if host in url:
            return name
    return "other"


def default_proxy(url: str) -> bool:
    """这个链接默认走不走代理。"""
    url = (url or "").lower()
    return not any(host in url for host in DOMESTIC_HOSTS)


def bilibili_room(url: str) -> Optional[str]:
    found = BILIBILI_ROOM.search(url or "")
    return found.group(1) if found else None


def parse_bilibili(payload: Dict[str, Any]) -> Dict[str, Any]:
    if payload.get("code") != 0 or not isinstance(payload.get("data"), dict):
        raise RuntimeError(f"B 站房间接口返回错误：{payload.get('message') or payload.get('msg') or payload.get('code')}")
    data = payload["data"]
    return {"live": data.get("live_status") == 1, "title": data.get("title") or "",
            "status_code": data.get("live_status")}


def probe_bilibili(room: str, proxy: Optional[str] = None) -> Dict[str, Any]:
    response = httpx.get(BILIBILI_API, params={"room_id": room}, proxy=proxy, timeout=15,
                         headers={"User-Agent": "Mozilla/5.0", "Referer": "https://live.bilibili.com/"})
    response.raise_for_status()
    return parse_bilibili(response.json())


def parse_streamlink_json(text: str) -> Dict[str, Any]:
    data = json.loads(text)
    if data.get("error"):
        error = str(data["error"])
        if any(word in error.lower() for word in STREAMLINK_OFFLINE):
            return {"live": False, "title": "", "error": error}
        raise RuntimeError(error)
    meta = data.get("metadata") or {}
    return {"live": bool(data.get("streams")), "title": meta.get("title") or ""}


def probe_streamlink(url: str, proxy: Optional[str] = None) -> Dict[str, Any]:
    command = [sys.executable, "-m", "streamlink", "--json"]
    if proxy:
        command += ["--http-proxy", proxy]
    creationflags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    result = subprocess.run(command + [url], capture_output=True, text=True, timeout=PROBE_TIMEOUT,
                            encoding="utf-8", errors="replace", creationflags=creationflags)
    return parse_streamlink_json(result.stdout or "{}")


def probe(url: str, proxy: Optional[str] = None) -> Dict[str, Any]:
    """{"live": True/False/None, "title": ..., "error": ...}；查询失败时 live 为 None。"""
    try:
        room = bilibili_room(url)
        return probe_bilibili(room, proxy) if room else probe_streamlink(url, proxy)
    except Exception as error:
        logger.warning("探测直播状态失败 %s：%s", url, error)
        return {"live": None, "title": "", "error": str(error)[:200]}
