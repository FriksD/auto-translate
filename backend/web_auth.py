"""
公网访问（内网穿透）的边界和飞书登录。

服务本身只监听 127.0.0.1，经 ngrok 之类的穿透进来的请求会带上外部域名的 Host 头和 X-Forwarded-For。
这类请求只能访问精校页相关的路径，设置、API Key、任务管理等接口一律 404；
精校接口还要求飞书登录，并且登录的人在配置的飞书群里。本机直接访问不受影响、不需要登录。
"""
import base64
import hashlib
import hmac
import json
import secrets
import threading
import time
from typing import Any, Dict, Optional, Set
from urllib.parse import urlencode

import httpx

from . import config
from .integrations import feishu

LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}
# 公网请求能访问的路径前缀
PUBLIC_PREFIXES = ("/review/", "/assets/", "/api/review/", "/auth/feishu/")
SESSION_COOKIE = "atn_session"
STATE_COOKIE = "atn_oauth_state"
SESSION_DAYS = 30
AUTHORIZE_URL = "https://accounts.feishu.cn/open-apis/authen/v1/authorize"
# 群成员名单的缓存时间；名单里没有的人，最多隔这么久再查一次（刚进群的人）
MEMBERS_TTL = 300
MEMBERS_RETRY = 30


def _host(headers: Any) -> str:
    host = (headers.get("host") or "").strip().lower()
    if host.startswith("["):
        return host[1:host.find("]")]
    return host.rsplit(":", 1)[0] if host.count(":") == 1 else host


def is_public(headers: Any) -> bool:
    """请求是不是经内网穿透从外部进来的。"""
    return _host(headers) not in LOCAL_HOSTS or "x-forwarded-for" in headers


def public_path_allowed(path: str) -> bool:
    return path.startswith(PUBLIC_PREFIXES)


def safe_next(path: Optional[str]) -> str:
    """登录后跳回的页面，只允许站内的精校页，避免被拿来做任意跳转。"""
    return path if path and path.startswith("/review/") and "//" not in path else "/review/"


# ── 会话 ──────────────────────────────────────────────────────────────────────

_secret: Optional[bytes] = None


def _key() -> bytes:
    global _secret
    if _secret is None:
        path = config.DATA_DIR / "session.key"
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(secrets.token_hex(32), encoding="utf-8")
        _secret = path.read_text(encoding="utf-8").strip().encode()
    return _secret


def _sign(payload: bytes) -> str:
    return hmac.new(_key(), payload, hashlib.sha256).hexdigest()


def make_session(user: Dict[str, Any], now: Optional[float] = None) -> str:
    data = {**user, "exp": (now or time.time()) + SESSION_DAYS * 86400}
    payload = base64.urlsafe_b64encode(json.dumps(data, ensure_ascii=False).encode("utf-8"))
    return f"{payload.decode()}.{_sign(payload)}"


def read_session(cookie: Optional[str], now: Optional[float] = None) -> Optional[Dict[str, Any]]:
    if not cookie or "." not in cookie:
        return None
    payload, signature = cookie.rsplit(".", 1)
    if not hmac.compare_digest(_sign(payload.encode()), signature):
        return None
    try:
        data = json.loads(base64.urlsafe_b64decode(payload.encode()))
    except ValueError:
        return None
    if data.get("exp", 0) < (now or time.time()) or not data.get("open_id"):
        return None
    return data


# ── 飞书 OAuth ────────────────────────────────────────────────────────────────

def authorize_url(app_id: str, redirect_uri: str, state: str) -> str:
    return f"{AUTHORIZE_URL}?{urlencode({'client_id': app_id, 'redirect_uri': redirect_uri, 'state': state})}"


def new_state() -> str:
    return secrets.token_urlsafe(16)


def fetch_user(feishu_settings: Dict[str, Any], code: str, redirect_uri: str) -> Dict[str, Any]:
    """用授权码换 user_access_token，再取用户信息：{open_id, name, avatar}。"""
    with httpx.Client(base_url=feishu.BASE_URL, timeout=15, trust_env=False) as client:
        response = client.post("/authen/v2/oauth/token", json={
            "grant_type": "authorization_code",
            "client_id": feishu_settings.get("app_id", ""),
            "client_secret": feishu_settings.get("app_secret", ""),
            "code": code,
            "redirect_uri": redirect_uri,
        })
        token_data = response.json()
        token = token_data.get("access_token") or (token_data.get("data") or {}).get("access_token")
        if not token:
            raise feishu.FeishuError(f"飞书登录失败：{token_data.get('error_description') or token_data.get('msg') or token_data}")
        info = feishu.FeishuClient._check(client.get("/authen/v1/user_info",
                                                      headers={"Authorization": f"Bearer {token}"}))["data"]
    return {"open_id": info["open_id"], "name": info.get("name") or "飞书用户", "avatar": info.get("avatar_thumb")}


class GroupMembers:
    """配置的飞书群的成员（open_id），带缓存。接收方配置成个人时，只有那个人能访问。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._members: Set[str] = set()
        self._key: Optional[tuple] = None
        self._fetched_at = 0.0

    def _fetch(self, feishu_settings: Dict[str, Any]) -> Set[str]:
        if feishu_settings.get("receive_id_type") != "chat_id":
            return {feishu_settings.get("receive_id", "")}
        client = feishu.client_for({"feishu": feishu_settings})
        members: Set[str] = set()
        page_token = ""
        with client._client() as http:
            while True:
                params = {"member_id_type": "open_id", "page_size": 100}
                if page_token:
                    params["page_token"] = page_token
                data = client._check(http.get(f"/im/v1/chats/{feishu_settings['receive_id']}/members",
                                              params=params, headers=client._headers()))["data"]
                members.update(item["member_id"] for item in data.get("items") or [])
                page_token = data.get("page_token") or ""
                if not data.get("has_more") or not page_token:
                    return members

    def contains(self, feishu_settings: Dict[str, Any], open_id: str) -> bool:
        key = tuple(feishu_settings.get(k, "") for k in ("app_id", "receive_id_type", "receive_id"))
        with self._lock:
            age = time.time() - self._fetched_at
            stale = key != self._key or age > MEMBERS_TTL or (open_id not in self._members and age > MEMBERS_RETRY)
            if stale:
                try:
                    self._members = self._fetch(feishu_settings)
                    self._key, self._fetched_at = key, time.time()
                except Exception:
                    if key != self._key:
                        raise
                    # 查询失败时沿用上次的名单
            return open_id in self._members


members = GroupMembers()
