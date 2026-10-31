"""
飞书长连接：接收卡片按钮回调（card.action.trigger）和发给机器人的消息（im.message.receive_v1）。

长连接由本地服务主动连到飞书，不需要公网地址。开发者后台需要：
- 「事件与回调 → 回调配置」选择「使用长连接接收回调」，添加「卡片回传交互」；
- 「事件与回调 → 事件配置」选择「使用长连接接收事件」，添加「接收消息 v2.0」，
  并开通读取群里 @机器人消息、单聊消息的权限。

lark-oapi 的长连接客户端会一直阻塞，在单独的线程里跑；它在模块级别保存了一个事件循环，
这里换成线程自己的循环，避免和 uvicorn 的循环冲突。回调要在 3 秒内响应；
消息事件交给 on_message 后立即返回，不能卡住长连接。
"""
import asyncio
import json
import logging
import threading
from collections import deque
from typing import Any, Callable, Deque, Dict, Optional

logger = logging.getLogger(__name__)

# on_action(value, form_value, operator_open_id, message_id) -> 提示文字（None 表示不提示）；抛出异常时提示错误信息
ActionHandler = Callable[[Dict[str, Any], Dict[str, Any], str, str], Optional[str]]
# on_message({message_id, chat_id, chat_type, text, sender}) -> None，必须立即返回
MessageHandler = Callable[[Dict[str, Any]], None]


class FeishuEventClient:
    def __init__(self, on_action: ActionHandler, on_message: Optional[MessageHandler] = None):
        self.on_action = on_action
        self.on_message = on_message
        self._thread: Optional[threading.Thread] = None
        # 飞书没收到确认时会重推消息，按 message_id 去重
        self._seen: Deque[str] = deque(maxlen=500)

    def start(self, feishu_settings: Dict[str, Any]) -> None:
        if self._thread or not feishu_settings.get("enabled"):
            return
        app_id, app_secret = feishu_settings.get("app_id"), feishu_settings.get("app_secret")
        if not (app_id and app_secret):
            return
        self._thread = threading.Thread(target=self._run, args=(app_id, app_secret),
                                        name="feishu-ws", daemon=True)
        self._thread.start()

    def _run(self, app_id: str, app_secret: str) -> None:
        try:
            import lark_oapi as lark
            import lark_oapi.ws.client as ws_client
        except ImportError:
            logger.warning("未安装 lark-oapi，飞书卡片按钮和机器人命令不可用（pip install -r requirements.txt）")
            self._thread = None
            return
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        ws_client.loop = loop
        handler = (lark.EventDispatcherHandler.builder("", "")
                   .register_p2_card_action_trigger(self._on_action)
                   .register_p2_im_message_receive_v1(self._on_message)
                   .build())
        client = lark.ws.Client(app_id, app_secret, event_handler=handler, log_level=lark.LogLevel.WARNING)
        try:
            client.start()
        except Exception:
            logger.exception("飞书长连接失败，卡片按钮和机器人命令不可用；检查飞书设置后点「发送测试卡片」可重连")
        finally:
            self._thread = None

    def _on_action(self, data: Any) -> Any:
        from lark_oapi.event.callback.model.p2_card_action_trigger import P2CardActionTriggerResponse

        event = data.event
        action = event.action if event else None
        value = (action.value if action else None) or {}
        form = (action.form_value if action else None) or {}
        operator = (event.operator.open_id if event and event.operator else None) or ""
        message_id = (event.context.open_message_id if event and event.context else None) or ""
        try:
            message = self.on_action(value, form, operator, message_id)
            toast = {"type": "success", "content": message} if message else None
        except Exception as error:
            detail = getattr(error, "detail", None) or str(error) or type(error).__name__
            toast = {"type": "error", "content": str(detail)[:200]}
        return P2CardActionTriggerResponse({"toast": toast} if toast else {})

    def _on_message(self, data: Any) -> None:
        message = data.event.message if data and data.event else None
        if message is None or self.on_message is None:
            return
        if message.message_id in self._seen:
            return
        self._seen.append(message.message_id)
        sender = data.event.sender
        if sender and sender.sender_type and sender.sender_type != "user":
            return
        if message.message_type != "text":
            text = ""
        else:
            try:
                text = json.loads(message.content or "{}").get("text") or ""
            except ValueError:
                text = ""
        try:
            self.on_message({
                "message_id": message.message_id,
                "chat_id": message.chat_id,
                "chat_type": message.chat_type,
                "text": text,
                "sender": (sender.sender_id.open_id if sender and sender.sender_id else None) or "",
            })
        except Exception:
            logger.exception("处理飞书消息出错")
