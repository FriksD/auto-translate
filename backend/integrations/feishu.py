"""
飞书自建应用：发送文本通知和文件。

第一阶段只需要主动发消息，直接调用开放平台 HTTP 接口；
后续需要卡片回调时再接入 lark-oapi 的长连接。
"""
import json
import logging
import time
from pathlib import Path
from typing import Any, Dict, Optional

import httpx

logger = logging.getLogger(__name__)

BASE_URL = "https://open.feishu.cn/open-apis"


class FeishuError(RuntimeError):
    pass


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
            raise FeishuError(f"飞书接口错误 {data.get('code')}: {data.get('msg')}")
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

    def _send(self, msg_type: str, content: Dict[str, Any]) -> None:
        with self._client() as client:
            self._check(client.post(
                "/im/v1/messages",
                params={"receive_id_type": self.receive_id_type},
                headers=self._headers(),
                json={
                    "receive_id": self.receive_id,
                    "msg_type": msg_type,
                    "content": json.dumps(content, ensure_ascii=False),
                },
            ))

    def send_text(self, text: str) -> None:
        self._send("text", {"text": text})

    def send_file(self, path: Path) -> None:
        file_type = "doc" if path.suffix.lower() in (".doc", ".docx") else "stream"
        with self._client() as client, open(path, "rb") as fh:
            data = self._check(client.post(
                "/im/v1/files",
                headers=self._headers(),
                data={"file_type": file_type, "file_name": path.name},
                files={"file": (path.name, fh)},
            ))
        self._send("file", {"file_key": data["data"]["file_key"]})


def notify(settings: Dict[str, Any], text: str) -> bool:
    """发送一条文本通知；未启用或发送失败时只记日志，不抛异常。"""
    feishu = settings.get("feishu") or {}
    if not feishu.get("enabled"):
        return False
    try:
        FeishuClient(feishu).send_text(text)
        return True
    except Exception as error:
        logger.warning("飞书通知发送失败: %s", error)
        return False
