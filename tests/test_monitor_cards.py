"""飞书直播间卡片、采访任务卡片精简、「直播间」「监视」命令的测试。"""
import asyncio
import sys
import tempfile
import time
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from backend import config, db
from backend.integrations import feishu, monitor_cards, schedule_card, task_cards
from backend.pipeline import monitor
from backend.runner import State

from test_task_cards import FakeClient, callbacks, text_of

B35 = "https://live.bilibili.com/35"
B34 = "https://live.bilibili.com/34"
YT = "https://www.youtube.com/watch?v=abc"


def status_of(**extra):
    now = time.time()
    status = {"id": 3, "title": "IEM 北京", "event_name": "IEM Beijing 2026", "enabled": True, "windows": [],
              "on": True, "streams": [{"url": B35}],
              "stream_status": [{"url": B35, "label": "", "proxy": False, "platform": "bilibili",
                                 "status": "recording", "title": "主舞台", "recording": True, "connected": True,
                                 "origin": now - 3600, "shot_runs": [{"start": 3000, "end": 3100, "open": False}],
                                 "detection": {"active": {"start": 3500, "interviewees": ["ZywOo"],
                                                          "team": "Vitality"}}}],
              "events": [{"ts": now, "level": "error", "message": "[B站] 拉不到流"}],
              "tasks": [{"id": 11, "title": "ZywOo 采访", "state": State.REVIEW, "created_at": now - 60},
                        {"id": 9, "title": "昨天的", "state": State.DONE, "created_at": now - 3 * 86400}]}
    status.update(extra)
    return status


class ReplaceStreamTest(unittest.TestCase):
    def test_add_replace_and_proxy(self):
        self.assertEqual(monitor_cards.replace_stream([], B35), [{"url": B35}])
        two = monitor_cards.replace_stream([{"url": B35}], YT, proxy="on")
        self.assertEqual(two, [{"url": B35}, {"url": YT, "proxy": True}])
        # 满两条：替换同平台的那条
        replaced = monitor_cards.replace_stream(two, B34)
        self.assertEqual([s["url"] for s in replaced], [B34, YT])
        # 指定第 2 条
        self.assertEqual([s["url"] for s in monitor_cards.replace_stream(two, B34, "1")], [B35, B34])
        # 已有这条：只改代理
        same = monitor_cards.replace_stream([{"url": B35, "proxy": True}], B35, proxy="off")
        self.assertEqual(same, [{"url": B35, "proxy": False}])
        self.assertEqual(monitor_cards.replace_stream([{"url": B35, "proxy": True}], B35, proxy="auto"), [{"url": B35}])
        # 换成列表里另一条已有的链接：不留重复
        self.assertEqual([s["url"] for s in monitor_cards.replace_stream(two, YT, "0")], [YT])


class CommandTest(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(schedule_card.parse_command("@_user_1 直播间"), ("monitors", {}))
        self.assertEqual(schedule_card.parse_command(f"监视 {B35} IEM Beijing 2026"),
                         ("watch", {"url": B35, "name": "IEM Beijing 2026"}))
        self.assertEqual(schedule_card.parse_command(f"监视 IEM北京 {B35}"), ("watch", {"url": B35, "name": "IEM北京"}))
        self.assertEqual(schedule_card.parse_command("监视"), ("watch_usage", {}))
        self.assertIn("监视 <直播链接>", schedule_card.HELP_TEXT)


class MonitorCardTest(unittest.TestCase):
    def test_on_card(self):
        card = monitor_cards.build_monitor_card(status_of())
        text = text_of(card)
        self.assertIn("录制中", text)
        self.assertIn("正在采访 ZywOo（Vitality）", text)
        self.assertIn("最近的采访镜头", text)
        self.assertIn("今天截到的采访**（1）", text)
        self.assertNotIn("昨天的", text)
        self.assertIn("拉不到流", text)
        self.assertEqual(callbacks(card), ["monitor_stream", "monitor_off"])
        self.assertEqual(card["header"]["text_tag_list"][0]["text"]["content"], "监视中")

    def test_off_card_and_reconnecting(self):
        status = status_of(on=False, enabled=False)
        status["stream_status"][0].update(status="stopped", recording=False, connected=False)
        card = monitor_cards.build_monitor_card(status)
        self.assertNotIn("正在采访", text_of(card))
        self.assertEqual(callbacks(card), ["monitor_stream", "monitor_on"])
        status = status_of()
        status["stream_status"][0]["connected"] = False
        self.assertIn("断流，正在重连", text_of(monitor_cards.build_monitor_card(status)))

    def test_window_subtitle(self):
        now = time.time()
        status = status_of(enabled=False, windows=[{"start": now + 3600, "end": now + 7200}])
        self.assertIn("按预约开始监视", monitor_cards.build_monitor_card(status, now)["header"]["subtitle"]["content"])


class InterviewTaskCardTest(unittest.TestCase):
    def _task(self, state):
        return {"id": 5, "title": "IEM Beijing 2026 ZywOo (Vitality) 赛后采访", "state": state, "stage": None,
                "source_type": "live", "error": None, "updated_at": time.time(), "created_at": time.time(),
                "context": {"monitor": {"id": 3, "event_name": "IEM Beijing 2026"},
                            "match": {"event": "IEM Beijing 2026", "score": [2, 1]},
                            "identified": {"interviewees": ["ZywOo"], "team": "Vitality", "opponent": "MOUZ",
                                           "about_match": "just_played"}}}

    def test_steps_buttons_and_lines(self):
        origin = time.time() - 3600
        state = {"recording": {"started_at": origin},
                 "interview": {"start": 100, "end": 400, "source": "auto"}}
        card = task_cards.build_card(self._task(State.PROCESSING), state)
        text = text_of(card)
        self.assertIn("✅ 采访 → 🔵 **初稿** → ⚪ 精校", text)
        self.assertIn("自动判定", text)
        self.assertIn("**识别** ZywOo（Vitality） · 赛后采访 · 对手 MOUZ", text)
        self.assertNotIn("比分", text)
        self.assertEqual(callbacks(card), [])
        self.assertIn("IEM Beijing 2026", card["header"]["subtitle"]["content"])
        review = task_cards.build_card(self._task(State.REVIEW), state)
        self.assertEqual(callbacks(review), ["done"])
        failed = task_cards.step_track(self._task(State.FAILED), state)
        self.assertEqual(failed[1], ("初稿", "failed"))
        


class ServiceAndActionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        config.use_data_dir(Path(self.tmp.name))
        db.init_db()
        db.save_settings({"feishu": {"enabled": True, "app_id": "a", "app_secret": "s", "receive_id": "oc_1"}})
        self.client = FakeClient()
        patcher = unittest.mock.patch.object(feishu, "client_for", return_value=self.client)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.service = monitor_cards.MonitorCardService()

    def tearDown(self):
        db._conn.close()
        self.tmp.cleanup()

    def test_only_on_monitors_get_cards_then_updates(self):
        off = db.create_monitor(title="关着的", streams=[{"url": B34, "proxy": False}])
        on = db.create_monitor(title="开着的", streams=[{"url": B35, "proxy": False}], enabled=True)
        self.service.sync_all()
        self.assertEqual(len(self.client.sent), 1)
        self.assertIsNone(db.get_monitor_card(off["id"]))
        self.service.sync_all()  # 没变化不更新
        self.assertEqual(self.client.updates, [])
        db.update_monitor(on["id"], enabled=False)
        self.service.sync_all()  # 关掉后更新成已停止
        self.assertEqual(len(self.client.updates), 1)
        self.assertIn("已停止", self.client.updates[0][1]["header"]["text_tag_list"][0]["text"]["content"])

    def test_alert_replies_in_thread(self):
        found = db.create_monitor(title="开着的", streams=[{"url": B35, "proxy": False}], enabled=True)
        self.service.alert(found["id"], "【直播间异常】拉不到流")
        self.assertEqual(self.client.replies, [("om_1", "【直播间异常】拉不到流")])

    def test_card_actions_and_watch_command(self):
        from backend import app

        found = db.create_monitor(title="IEM 北京", event_name="IEM Beijing 2026",
                                  streams=monitor.normalize_streams([{"url": B35}]))
        run = lambda coro: asyncio.run(coro)  # noqa: E731
        with unittest.mock.patch.object(app.monitor_cards, "sync") as sync:
            self.assertEqual(run(app._card_action({"action": "monitor_on", "monitor_id": found["id"]}, {}, "", "")),
                             "已开始监视")
            self.assertTrue(db.get_monitor(found["id"])["enabled"])
            run(app._card_action({"action": "monitor_stream", "monitor_id": found["id"]},
                                 {"url": YT, "slot": "new", "proxy": "auto"}, "", ""))
            streams = db.get_monitor(found["id"])["streams"]
            self.assertEqual([(s["url"], s["proxy"]) for s in streams], [(B35, False), (YT, True)])
            db.update_monitor(found["id"], windows=[{"start": 0, "end": time.time() + 3600}])
            run(app._card_action({"action": "monitor_off", "monitor_id": found["id"]}, {}, "", ""))
            self.assertEqual((db.get_monitor(found["id"])["enabled"], db.get_monitor(found["id"])["windows"]),
                             (False, []))

            # 按赛事名找到已有的直播间，换掉同平台的链接
            reply = run(app._watch(B34, "IEM Beijing 2026"))
            self.assertIn("已开始监视「IEM 北京」", reply)
            self.assertEqual([s["url"] for s in db.get_monitor(found["id"])["streams"]], [B34, YT])
            # 没有赛事名、也没有开着的别的直播间：加到这个开着的
            run(app._watch(B35, ""))
            self.assertEqual(len(db.list_monitors()), 1)
            # 新赛事：新建
            reply = run(app._watch(B35, "BLAST Rivals"))
            self.assertEqual(len(db.list_monitors()), 2)
            self.assertIn("BLAST Rivals", reply)
            self.assertIn("「IEM 北京」已停止监视", reply)
            self.assertFalse(db.get_monitor(found["id"])["enabled"])
            self.assertIn("已把 2 个直播间的卡片", run(app._send_monitor_cards()))
        self.assertTrue(all(call.args[1] is True for call in sync.call_args_list))



class ReservationTest(unittest.TestCase):
    def test_window_and_lookup(self):
        cfg = {"pre_match_minutes": 15, "window_hours": 5}
        window = monitor.reservation_window(7, 10_000.0, "A vs B", cfg, now=5_000.0, reserved_by="ou_1")
        self.assertEqual((window["start"], window["end"]), (10_000 - 900, 10_000 + 5 * 3600))
        unknown = monitor.reservation_window(8, None, "C vs D", cfg, now=5_000.0)
        self.assertEqual((unknown["start"], unknown["end"]), (5_000.0, 5_000 + 5 * 3600))
        monitors = [{"id": 1, "title": "IEM 北京", "event_id": "8000", "event_name": "IEM Beijing 2026", "windows": [window]},
                    {"id": 2, "title": "BLAST", "event_id": None, "event_name": "BLAST Rivals Fall 2026", "windows": []}]
        self.assertEqual(monitor.find_reservation(monitors, 7)[0]["id"], 1)
        self.assertIsNone(monitor.find_reservation(monitors, 9))
        self.assertEqual(monitor.find_event_monitor(monitors, 8000, "whatever")["id"], 1)
        self.assertEqual(monitor.find_event_monitor(monitors, 1, "Intel Extreme Masters Beijing 2026")["id"], 1)
        self.assertEqual(monitor.find_event_monitor(monitors, None, "blast rivals fall 2026")["id"], 2)
        self.assertIsNone(monitor.find_event_monitor(monitors, None, "PGL Masters"))
        self.assertEqual(monitor.drop_reservation([window, unknown], 7), [unknown])
        # 窗口内就算开着
        self.assertTrue(monitor.is_on({"enabled": False, "windows": [window]}, 10_000.0))
        self.assertFalse(monitor.is_on({"enabled": False, "windows": [window]}, 1_000.0))

    def test_official_stream(self):
        streams = [{"url": "https://twitch.tv/es_cs", "provider": "twitch", "country": "Spain", "viewers": 9000},
                   {"url": "https://www.youtube.com/watch?v=x", "provider": "youtube", "country": "Other", "viewers": 1},
                   {"url": "https://www.youtube.com/@ESLCS", "provider": "youtube", "country": "Other", "viewers": 1},
                   {"url": "https://twitch.tv/eslcs", "provider": "twitch", "country": "Other", "viewers": 5}]
        self.assertEqual(monitor.official_stream(streams)["url"], "https://twitch.tv/eslcs")
        self.assertEqual(monitor.official_stream(streams[1:3])["url"], "https://www.youtube.com/@ESLCS")
        self.assertIsNone(monitor.official_stream([{"url": "x", "provider": "afreeca"}]))


class MigrationTest(unittest.TestCase):
    def test_retired_states_become_cancelled(self):
        with tempfile.TemporaryDirectory() as tmp:
            config.use_data_dir(Path(tmp))
            db.init_db()
            old = db.create_task(title="old", state="recording", source_type="live")
            kept = db.create_task(title="kept", state=State.REVIEW, source_type="live")
            db._conn.close()
            db.init_db()
            self.assertEqual(db.get_task(old["id"])["state"], State.CANCELLED)
            self.assertEqual(db.get_task(kept["id"])["state"], State.REVIEW)
            self.assertIn("直播间监视", db.list_events(old["id"])[-1]["message"])
            db._conn.close()


if __name__ == "__main__":
    unittest.main()
