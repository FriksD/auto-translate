"""
飞书长连接：接收卡片按钮回调（card.action.trigger）。

长连接由本地服务主动连到飞书，不需要公网地址。需要在开发者后台「事件与回调 → 回调配置」里
选择「使用长连接接收回调」，并添加「卡片回传交互」（card.action.trigger）。

lark-oapi 的长连接客户端会一直阻塞，在单独的线程里跑；它在模块级别保存了一个事件循环，
这里换成线程自己的循环，避免和 uvicorn 的循环冲突。飞书要求 3 秒内响应。
"""
import asyncio
import logging
import threading
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)

# handler(action, task_id, form_value, operator_open_id) -> 提示文字；抛出异常时提示错误信息
ActionHandler = Callable[[str, int, Dict[str, Any], str], str]


class CardCallbackClient:
    def __init__(self, handler: ActionHandler):
        self.handler = handler
        self._thread: Optional[threading.Thread] = None

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
            logger.warning("未安装 lark-oapi，飞书卡片按钮不可用（pip install -r requirements.txt）")
            self._thread = None
            return
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        ws_client.loop = loop
        handler = (lark.EventDispatcherHandler.builder("", "")
                   .register_p2_card_action_trigger(self._on_action)
                   .build())
        client = lark.ws.Client(app_id, app_secret, event_handler=handler, log_level=lark.LogLevel.WARNING)
        try:
            client.start()
        except Exception:
            logger.exception("飞书长连接失败，卡片按钮不可用；检查飞书设置后点「发送测试卡片」可重连")
        finally:
            self._thread = None

    def _on_action(self, data: Any) -> Any:
        from lark_oapi.event.callback.model.p2_card_action_trigger import P2CardActionTriggerResponse

        event = data.event
        value = (event.action.value if event and event.action else None) or {}
        form = (event.action.form_value if event and event.action else None) or {}
        operator = (event.operator.open_id if event and event.operator else None) or ""
        try:
            message = self.handler(str(value.get("action") or ""), int(value.get("task_id")), form, operator)
            toast = {"type": "success", "content": message}
        except Exception as error:
            detail = getattr(error, "detail", None) or str(error) or type(error).__name__
            toast = {"type": "error", "content": str(detail)[:200]}
        return P2CardActionTriggerResponse({"toast": toast})
