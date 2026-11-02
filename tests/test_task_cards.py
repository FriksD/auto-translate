"""飞书任务卡片：卡片内容、发送与更新、话题提醒、按钮回调的测试。"""
import json
import sys
import tempfile
import time
import unittest
import unittest.mock
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend import config, db
from backend.config import DEFAULT_SETTINGS
from backend.integrations import feishu, task_cards
from backend.integrations.feishu_ws import FeishuEventClient
from backend.pipeline import live
from backend.runner import State

CFG = DEFAULT_SETTINGS["live"]


def make_task(state=State.RECORDING, source_type="live", **extra):
    task = {"id": 7, "title": "Vitality vs MOUZ · IEM Chengdu", "state": state, "stage": None,
            "source_type": source_type, "match_time": time.time() + 3600, "error": None, "updated_at": time.time(),
            "context": {"match": {"event": "IEM Chengdu", "format": "Best of 3", "unix_ms": 1_700_000_000_000}}}
    task.update(extra)
    return task


def callbacks(card):
    """卡片里所有回调按钮的 action。"""
    found = []

    def walk(node):
        if isinstance(node, dict):
            for behavior in node.get("behaviors") or []:
                if behavior.get("type") == "callback":
                    found.append(behavior["value"]["action"])
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)
    walk(card)
    return found


def text_of(card):
    return "\n".join(e["content"] for e in card["body"]["elements"] if e["tag"] == "markdown")


class BuildCardTest(unittest.TestCase):
    def test_recording_card(self):
        state = {
            "stream": {"name": "ESL_CS", "url": "https://twitch.tv/esl_cs"},
            "recording": {"started_at": time.time() - 600},
            "snapshot": {"summary": "Vitality 1:1 MOUZ（Inferno 9:11）", "reason": "决胜图赛点方 11 分",
                         "next_check_at": time.time() + 60,
                         "maps": [{"name": "Mirage", "team1_score": 13, "team2_score": 9, "status": "finished"},
                                  {"name": "Nuke", "team1_score": 10, "team2_score": 13, "status": "finished"},
                                  {"name": "Inferno", "team1_score": None, "team2_score": None, "status": "live"}]},
        }
        card = task_cards.build_card(make_task(), state, cfg=CFG)
        self.assertEqual(card["schema"], "2.0")
        self.assertTrue(card["config"]["update_multi"])
        self.assertEqual(card["header"]["template"], "red")
        self.assertIn("录制中", card["config"]["summary"]["content"])
        body = text_of(card)
        self.assertIn("🔴 **录制**", body)
        self.assertIn("Mirage 13:9 · Nuke 10:13 · Inferno ▶", body)
        self.assertIn("ESL_CS", body)
        self.assertEqual(callbacks(card), ["stream", "check", "match_over", "no_interview"])
        self.assertLess(len(json.dumps(card, ensure_ascii=False).encode("utf-8")), 30 * 1024)

    def test_reserved_card_buttons(self):
        card = task_cards.build_card(make_task(State.RESERVED), {}, cfg=CFG)
        self.assertEqual(callbacks(card), ["check", "record", "cancel"])
        cancel = card["body"]["elements"][-1]["columns"][2]["elements"][0]
        self.assertIn("confirm", cancel)
        self.assertIn("开赛前 15 分钟开始监视", text_of(card))

    def test_review_card_shows_suspects(self):
        report = {"suspects": [{"token": "Shearer", "candidates": ["sh1ro"]}], "unknown": []}
        card = task_cards.build_card(make_task(State.REVIEW), {"interview": {"start": 60, "end": 300, "source": "auto"}},
                                     names_report=report, cfg=CFG)
        self.assertEqual(callbacks(card), ["done"])
        self.assertIn("Shearer（可能是 sh1ro）", text_of(card))
        self.assertIn("录音 01:00–05:00（自动判定）", text_of(card))
        self.assertEqual(card["header"]["template"], "orange")

    def test_reserved_by_mention(self):
        card = task_cards.build_card(make_task(State.RESERVED), {}, cfg=CFG, reserved_by="ou_abc")
        self.assertIn("<at id=ou_abc></at>", text_of(card))

    def test_events_skip_pipeline_noise(self):
        events = [{"ts": time.time(), "level": "info", "message": "开始：语音转写"},
                  {"ts": time.time(), "level": "warning", "message": "直播流中断，5 秒后重连"},
                  {"ts": time.time(), "level": "info", "message": "完成：语音转写"}]
        card = task_cards.build_card(make_task(State.PROCESSING, stage="transcribe"), {}, events=events, cfg=CFG)
        body = text_of(card)
        self.assertIn("⚠️ 直播流中断", body)
        self.assertNotIn("开始：语音转写", body)
        self.assertIn("正在语音转写", body)

    def test_step_track(self):
        track = task_cards.step_track(make_task(State.NO_INTERVIEW), {"recording": {}, "match_over_at": 1})
        self.assertEqual([m for _, m in track], ["done", "done", "done", "stopped", "todo", "todo"])
        track = task_cards.step_track(make_task(State.FAILED, stage="clean"), {"interview": {}})
        self.assertEqual(track[4], ("初稿", "failed"))
        track = task_cards.step_track(make_task(State.DONE), {})
        self.assertTrue(all(m == "done" for _, m in track))
        track = task_cards.step_track(make_task(State.PROCESSING, source_type="url"), {})
        self.assertEqual([m for _, m in track], ["done", "current", "todo"])


class FakeClient:
    def __init__(self):
        self.sent, self.updates, self.replies, self.files, self.texts = [], [], [], [], []
        self.update_error = None
        self.reply_error = None

    def send_card(self, card):
        self.sent.append(card)
        return f"om_{len(self.sent)}"

    def update_card(self, message_id, card):
        if self.update_error:
            raise self.update_error
        self.updates.append((message_id, card))

    def reply_text(self, message_id, text):
        if self.reply_error and "user_id=\"all\"" in text:
            raise self.reply_error
        self.replies.append((message_id, text))

    def reply_file(self, message_id, path):
        self.files.append((message_id, path))

    def send_text(self, text):
        self.texts.append(text)


class CardServiceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        config.use_data_dir(Path(self.tmp.name))
        db.init_db()
        db.save_settings({"feishu": {"enabled": True, "app_id": "a", "app_secret": "s", "receive_id": "oc_1"}})
        self.client = FakeClient()
        patcher = unittest.mock.patch.object(feishu, "client_for", return_value=self.client)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.service = task_cards.TaskCardService()
        self.task = db.create_task(title="A vs B", state=State.RESERVED, source_type="live", match_hltv_id="1")
        live.patch_state(self.task["id"], recording={"started_at": time.time()})  # 录过才发卡片

    def tearDown(self):
        db._conn.close()
        self.tmp.cleanup()

    def test_reserved_card_waits_for_match_start(self):
        task = db.create_task(title="C vs D", state=State.RESERVED, source_type="live", match_hltv_id="2")
        self.service.track(task["id"])
        self.service.sync_all()
        # 预约后立即抓背景信息：短暂处于处理中
        db.update_task(task["id"], state=State.PROCESSING, stage="context")
        self.service.sync_all()
        db.update_task(task["id"], state=State.WATCHING)
        live.patch_state(task["id"], snapshot={"status": "upcoming"})
        self.service.sync_all()
        self.assertEqual(self.client.sent, [])
        live.patch_state(task["id"], recording={"started_at": time.time()})
        db.update_task(task["id"], state=State.RECORDING)
        self.service.sync_all()
        self.assertEqual(len(self.client.sent), 1)

    def test_watching_card_sent_when_match_goes_live(self):
        task = db.create_task(title="C vs D", state=State.WATCHING, source_type="live", match_hltv_id="2")
        self.service.track(task["id"])
        self.service.sync_all()
        self.assertEqual(self.client.sent, [])
        live.patch_state(task["id"], snapshot={"status": "live"})
        self.service.sync_all()
        self.assertEqual(len(self.client.sent), 1)

    def test_cancelled_reservation_never_sends(self):
        task = db.create_task(title="C vs D", state=State.RESERVED, source_type="live", match_hltv_id="2")
        self.service.track(task["id"])
        db.update_task(task["id"], state=State.CANCELLED)
        self.service.sync_all()
        self.assertEqual(self.client.sent, [])

    def test_alert_before_recording_sends_card(self):
        task = db.create_task(title="C vs D", state=State.WATCHING, source_type="live", match_hltv_id="2")
        self.service.track(task["id"])
        self.service.alert(task["id"], "找不到直播源", "请在卡片里填写直播链接")
        self.assertEqual(len(self.client.sent), 1)
        self.assertEqual(self.client.replies, [("om_1", "【找不到直播源】请在卡片里填写直播链接")])

    def test_manual_task_card_sent_at_once(self):
        task = db.create_task(title="上传", state=State.DRAFT, source_type="file")
        self.service.track(task["id"])
        self.assertEqual(self.service.sync(task["id"]), "om_1")

    def test_send_once_then_update_on_change(self):
        self.service.track(self.task["id"])
        self.assertEqual(self.service.sync(self.task["id"]), "om_1")
        self.service.sync(self.task["id"])
        self.assertEqual((len(self.client.sent), len(self.client.updates)), (1, 0))  # 没变化不更新
        db.update_task(self.task["id"], state=State.WATCHING)
        self.service.sync_all()
        self.assertEqual(len(self.client.updates), 1)
        self.assertEqual(self.client.updates[0][1]["header"]["text_tag_list"][0]["text"]["content"], "监视中")

    def test_resend_when_card_expired(self):
        self.service.track(self.task["id"])
        self.service.sync(self.task["id"])
        db.update_task(self.task["id"], state=State.WATCHING)
        self.client.update_error = feishu.FeishuError("expired", 230031)
        self.assertEqual(self.service.sync(self.task["id"]), "om_2")
        self.assertEqual(db.get_card(self.task["id"])["message_id"], "om_2")

    def test_alert_replies_in_card_thread(self):
        self.service.track(self.task["id"])
        self.service.alert(self.task["id"], "找不到直播源", "请在卡片里填写直播链接")
        self.assertEqual(self.client.replies, [("om_1", "【找不到直播源】请在卡片里填写直播链接")])
        self.assertEqual(self.client.texts, [])

    def test_alert_mention_all(self):
        self.service.track(self.task["id"])
        self.service.alert(self.task["id"], "初稿就绪", "", mention_all=True)
        self.assertEqual(self.client.replies, [("om_1", '<at user_id="all"></at> 【初稿就绪】')])

    def test_alert_mention_all_refused_sends_plain(self):
        self.client.reply_error = feishu.FeishuError("no permission", 230027)
        self.service.track(self.task["id"])
        self.service.alert(self.task["id"], "比赛结束", "A 2:0 B", mention_all=True)
        self.assertEqual(self.client.replies, [("om_1", "【比赛结束】A 2:0 B")])

    def test_alert_without_card_falls_back_to_text(self):
        with unittest.mock.patch.object(feishu, "notify") as notify:
            self.service.alert(self.task["id"], "处理失败", "英文清洗失败")
        text = notify.call_args[0][1]
        self.assertIn("【处理失败】A vs B", text)
        self.assertIn("/tasks/", text)

    def test_disabled_does_not_track(self):
        db.save_settings({"feishu": {"enabled": False}})
        self.service.track(self.task["id"])
        self.assertIsNone(db.get_card(self.task["id"]))

    def test_quiet_task_not_rerendered(self):
        db.update_task(self.task["id"], state=State.DONE)
        self.service.track(self.task["id"])
        self.service.sync_all()
        with unittest.mock.patch.object(task_cards, "render", wraps=task_cards.render) as render:
            self.service.sync_all()
        render.assert_not_called()


class CallbackTest(unittest.TestCase):
    def event(self, value, form=None):
        action = SimpleNamespace(value=value, form_value=form)
        return SimpleNamespace(event=SimpleNamespace(action=action, operator=SimpleNamespace(open_id="ou_1"),
                                                     context=SimpleNamespace(open_message_id="om_9")))

    def test_dispatch_and_toast(self):
        calls = []
        client = FeishuEventClient(lambda *args: calls.append(args) or "已开始录制")
        value = {"action": "stream", "task_id": 7}
        response = client._on_action(self.event(value, {"url": "https://kick.com/x"}))
        self.assertEqual(calls, [(value, {"url": "https://kick.com/x"}, "ou_1", "om_9")])
        self.assertEqual((response.toast.type, response.toast.content), ("success", "已开始录制"))

    def test_no_toast_when_handler_returns_none(self):
        response = FeishuEventClient(lambda *args: None)._on_action(self.event({"action": "schedule"}))
        self.assertIsNone(response.toast)

    def test_error_toast(self):
        def fail(*_):
            raise RuntimeError("任务正在处理中")
        response = FeishuEventClient(fail)._on_action(self.event({"action": "record", "task_id": 7}))
        self.assertEqual((response.toast.type, response.toast.content), ("error", "任务正在处理中"))

    def test_message_dedup_and_text(self):
        received = []
        client = FeishuEventClient(lambda *args: None, received.append)
        message = SimpleNamespace(message_id="om_1", chat_id="oc_1", chat_type="group", message_type="text",
                                  content=json.dumps({"text": "@_user_1 赛程 明天"}))
        sender = SimpleNamespace(sender_type="user", sender_id=SimpleNamespace(open_id="ou_2"))
        data = SimpleNamespace(event=SimpleNamespace(message=message, sender=sender))
        client._on_message(data)
        client._on_message(data)  # 飞书重推
        self.assertEqual(received, [{"message_id": "om_1", "chat_id": "oc_1", "chat_type": "group",
                                     "text": "@_user_1 赛程 明天", "sender": "ou_2"}])


if __name__ == "__main__":
    unittest.main()
