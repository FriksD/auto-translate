"""
飞书自建应用：发送文本、文件和消息卡片，更新卡片，在卡片下回复话题。

主动发消息直接调用开放平台 HTTP 接口；卡片按钮回调走 lark-oapi 的长连接（见 feishu_ws.py）。
"""
import json
import logging
import time
from pathlib import Path
from typing import Any, Dict, Optional

import httpx

logger = logging.getLogger(__name__)

BASE_URL = "https://open.feishu.cn/open-apis"


# 卡片更新失败、只能重新发一张的错误码：超过 14 天不能再改、消息已撤回
CARD_GONE_CODES = (230031, 230011)


class FeishuError(RuntimeError):
    def __init__(self, message: str, code: Optional[int] = None):
        super().__init__(message)
        self.code = code


class FeishuClient:
    def __init__(self, settings: Dict[str, Any]):
        self.app_id = settings.get("app_id", "")
        self.app_secret = settings.get("app_secret", "")
        self.receive_id_type = settings.get("receive_id_type") or "chat_id"
        self.receive_id = settings.get("receive_id", "")
        self._token: Optional[str] = None
        self._token_expire = 0.0
        if not (self.app_id and self.app_secret and self.receive_id):
            raise FeishuError("飞书未配置完整（App ID / App Secret / 接收方 ID）")

    def _client(self) -> httpx.Client:
        # 飞书在国内，不走代理
        return httpx.Client(base_url=BASE_URL, timeout=30, trust_env=False)

    @staticmethod
    def _check(response: httpx.Response) -> Dict[str, Any]:
        try:
            data = response.json()
        except ValueError:
            raise FeishuError(f"HTTP {response.status_code}: {response.text[:200]}")
        if data.get("code", 0) != 0:
            raise FeishuError(f"飞书接口错误 {data.get('code')}: {data.get('msg')}", data.get("code"))
        return data

    def _tenant_token(self) -> str:
        if self._token and time.time() < self._token_expire - 60:
            return self._token
        with self._client() as client:
            data = self._check(client.post(
                "/auth/v3/tenant_access_token/internal",
                json={"app_id": self.app_id, "app_secret": self.app_secret},
            ))
        self._token = data["tenant_access_token"]
        self._token_expire = time.time() + data.get("expire", 7200)
        return self._token

    def _headers(self) -> Dict[str, str]:
        return {"Authorization": f"Bearer {self._tenant_token()}"}

    def _send(self, msg_type: str, content: Dict[str, Any]) -> str:
        """发到配置的群聊/个人，返回 message_id。"""
        with self._client() as client:
            data = self._check(client.post(
                "/im/v1/messages",
                params={"receive_id_type": self.receive_id_type},
                headers=self._headers(),
                json={
                    "receive_id": self.receive_id,
                    "msg_type": msg_type,
                    "content": json.dumps(content, ensure_ascii=False),
                },
            ))
        return data["data"]["message_id"]

    def _reply(self, message_id: str, msg_type: str, content: Dict[str, Any]) -> str:
        """以话题形式回复某条消息：会提醒群成员，但不在主聊天里刷屏。"""
        with self._client() as client:
            data = self._check(client.post(
                f"/im/v1/messages/{message_id}/reply",
                headers=self._headers(),
                json={
                    "msg_type": msg_type,
                    "content": json.dumps(content, ensure_ascii=False),
                    "reply_in_thread": True,
                },
            ))
        return data["data"]["message_id"]

    def _upload_file(self, path: Path) -> str:
        file_type = "doc" if path.suffix.lower() in (".doc", ".docx") else "stream"
        with self._client() as client, open(path, "rb") as fh:
            data = self._check(client.post(
                "/im/v1/files",
                headers=self._headers(),
                data={"file_type": file_type, "file_name": path.name},
                files={"file": (path.name, fh)},
            ))
        return data["data"]["file_key"]

    def send_text(self, text: str) -> None:
        self._send("text", {"text": text})

    def send_file(self, path: Path) -> None:
        self._send("file", {"file_key": self._upload_file(path)})

    def send_card(self, card: Dict[str, Any]) -> str:
        return self._send("interactive", card)

    def update_card(self, message_id: str, card: Dict[str, Any]) -> None:
        """整张替换已发出的卡片（卡片需声明 update_multi）。更新不会提醒群成员。"""
        with self._client() as client:
            self._check(client.patch(
                f"/im/v1/messages/{message_id}",
                headers=self._headers(),
                json={"content": json.dumps(card, ensure_ascii=False)},
            ))

    def reply_text(self, message_id: str, text: str) -> None:
        self._reply(message_id, "text", {"text": text})

    def reply_file(self, message_id: str, path: Path) -> None:
        self._reply(message_id, "file", {"file_key": self._upload_file(path)})


_clients: Dict[tuple, FeishuClient] = {}


def client_for(settings: Dict[str, Any]) -> FeishuClient:
    """按配置复用客户端，避免每次都重新获取 tenant_access_token。"""
    feishu = settings.get("feishu") or {}
    key = tuple(feishu.get(k, "") for k in ("app_id", "app_secret", "receive_id_type", "receive_id"))
    if key not in _clients:
        _clients.clear()
        _clients[key] = FeishuClient(feishu)
    return _clients[key]


def enabled(settings: Dict[str, Any]) -> bool:
    return bool((settings.get("feishu") or {}).get("enabled"))


def notify(settings: Dict[str, Any], text: str) -> bool:
    """发送一条文本通知；未启用或发送失败时只记日志，不抛异常。"""
    if not enabled(settings):
        return False
    try:
        client_for(settings).send_text(text)
        return True
    except Exception as error:
        logger.warning("飞书通知发送失败: %s", error)
        return False
