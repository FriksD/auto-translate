"""群里预约比赛：命令解析、赛程卡片、机器人命令处理的测试。"""
import asyncio
import json
import sys
import tempfile
import time
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend import config, db
from backend.integrations import feishu
from backend.integrations import schedule_card as S

NOW = time.mktime((2026, 10, 31, 12, 0, 0, 0, 0, -1))


def match(match_id, t1, t2, day, hour, stars=0, event="IEM Chengdu", window=None, live=False):
    unix_ms = int((NOW + day * 86400 + (hour - 12) * 3600) * 1000)
    return {"id": match_id, "unix_ms": unix_ms, "live": live, "stars": stars, "format": "bo3",
            "team1": {"name": t1}, "team2": {"name": t2}, "event": {"name": event},
            "window": window, "monitor_title": event if window else None}


MATCHES = [
    match(1, "Vitality", "MOUZ", 0, 19, stars=2),
    match(2, "Alter Ego", "NEXVOID", 0, 14),
    match(3, "Natus Vincere", None, 1, 18, stars=1, window={"start": 0, "end": 1}),
    match(4, "FURIA", "Spirit", 2, 20, stars=3, event="PGL Masters"),
]


def rows(card):
    """赛程卡片里的比赛行：(队伍文字, 右侧是按钮还是"已预约")"""
    found = []
    for element in card["body"]["elements"]:
        columns = element.get("columns") or []
        if len(columns) == 2 and columns[0].get("width") == "weighted":
            left = columns[0]["elements"][0]["content"]
            right = columns[1]["elements"][0]
            found.append((left.split("**")[1], right["tag"]))
    return found


def callback_values(card):
    values = []

    def walk(node):
        if isinstance(node, dict):
            for behavior in node.get("behaviors") or []:
                values.append(behavior["value"])
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)
    walk(card)
    return values


class ParseCommandTest(unittest.TestCase):
    def test_commands(self):
        self.assertEqual(S.parse_command("@_user_1"), ("help", {}))
        self.assertEqual(S.parse_command("@_user_1 赛程"), ("schedule", {"day": None, "stars": 0, "q": ""}))
        self.assertEqual(S.parse_command("赛程 明天")[1]["day"], 1)
        self.assertEqual(S.parse_command("比赛 全部")[1]["day"], S.DAY_ALL)
        self.assertEqual(S.parse_command("赛程 2星")[1]["stars"], 2)
        self.assertEqual(S.parse_command("预约  小蜜蜂 "), ("schedule", {"day": S.DAY_ALL, "stars": 0, "q": "小蜜蜂"}))
        self.assertEqual(S.parse_command("预约 https://www.hltv.org/matches/2399903/a-vs-b")[0], "reserve_url")
        self.assertEqual(S.parse_command("你好")[0], "unknown")


class ScheduleCardTest(unittest.TestCase):
    def test_default_today(self):
        card = S.build_schedule_card(MATCHES, {}, {}, fetched_at=NOW, now=NOW)
        self.assertEqual(rows(card), [("Alter Ego vs NEXVOID", "button"), ("Vitality vs MOUZ", "button")])
        self.assertTrue(card["config"]["update_multi"])
        self.assertIn("今天", card["header"]["title"]["content"])

    def test_reserved_and_tbd(self):
        card = S.build_schedule_card(MATCHES, {"day": 1}, {}, now=NOW)
        self.assertEqual(rows(card), [("Natus Vincere vs TBD", "button")])
        cancel = [v for v in callback_values(card) if v["action"] == "unreserve"]
        self.assertEqual(cancel[0]["match_id"], 3)
        self.assertIn("已预约 · IEM Chengdu", json.dumps(card, ensure_ascii=False))

    def test_stars_filter_and_buttons_keep_view(self):
        card = S.build_schedule_card(MATCHES, {"day": S.DAY_ALL, "stars": 2}, {}, now=NOW)
        self.assertEqual([r[0] for r in rows(card)], ["Vitality vs MOUZ", "FURIA vs Spirit"])
        reserve = [v for v in callback_values(card) if v["action"] == "reserve"]
        self.assertEqual(reserve[0]["match_id"], 1)
        self.assertEqual(reserve[0]["view"]["stars"], 2)
        self.assertEqual(reserve[0]["view"]["day"], S.DAY_ALL)

    def test_search_with_alias(self):
        card = S.build_schedule_card(MATCHES, {"day": S.DAY_ALL, "q": "小蜜蜂"}, {"小蜜蜂": "Vitality"}, now=NOW)
        self.assertEqual([r[0] for r in rows(card)], ["Vitality vs MOUZ"])
        card = S.build_schedule_card(MATCHES, {"day": S.DAY_ALL, "q": "pgl"}, {}, now=NOW)
        self.assertEqual([r[0] for r in rows(card)], ["FURIA vs Spirit"])

    def test_many_matches_capped_and_small(self):
        many = [match(100 + i, f"Team{i}", f"Rival{i}", 0, 13, event="Some Very Long Event Name Qualifier 2026")
                for i in range(60)]
        card = S.build_schedule_card(many, {}, {}, now=NOW)
        self.assertEqual(len(rows(card)), S.MAX_ROWS)
        self.assertIn("还有 35 场", json.dumps(card, ensure_ascii=False))
        self.assertLess(len(json.dumps(card, ensure_ascii=False).encode("utf-8")), 30 * 1024)

    def test_day_label(self):
        self.assertEqual(S.day_label("2026-10-31", NOW), "今天 10-31")
        self.assertEqual(S.day_label("2026-11-03", NOW), "11-03 周二")


class FakeClient:
    def __init__(self):
        self.replies, self.cards, self.updates = [], [], []

    def reply_text(self, message_id, text, in_thread=True):
        self.replies.append((message_id, text, in_thread))

    def reply_card(self, message_id, card, in_thread=False):
        self.cards.append((message_id, card))
        return "om_card"

    def update_card(self, message_id, card):
        self.updates.append((message_id, card))


class CommandHandlingTest(unittest.TestCase):
    def setUp(self):
        from backend import app as app_module

        self.app = app_module
        self.tmp = tempfile.TemporaryDirectory()
        config.use_data_dir(Path(self.tmp.name))
        db.init_db()
        db.save_settings({"feishu": {"enabled": True, "app_id": "a", "app_secret": "s",
                                     "receive_id_type": "chat_id", "receive_id": "oc_group"}})
        self.client = FakeClient()
        for patcher in (
            unittest.mock.patch.object(feishu, "client_for", return_value=self.client),
            unittest.mock.patch.object(app_module, "list_matches", unittest.mock.AsyncMock(
                return_value={"matches": MATCHES, "fetched_at": NOW})),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def tearDown(self):
        db._conn.close()
        self.tmp.cleanup()

    def handle(self, text, chat_id="oc_group", chat_type="group"):
        asyncio.run(self.app._handle_command({"message_id": "om_cmd", "chat_id": chat_id, "chat_type": chat_type,
                                              "text": text, "sender": "ou_me"}))

    def test_schedule_reply_card(self):
        self.handle("@_user_1 赛程")
        self.assertEqual(len(self.client.cards), 1)
        self.assertEqual(self.client.cards[0][0], "om_cmd")

    def test_other_group_ignored(self):
        self.handle("@_user_1 赛程", chat_id="oc_other")
        self.assertEqual((self.client.cards, self.client.replies), ([], []))
        self.handle("赛程", chat_id="oc_dm", chat_type="p2p")
        self.assertEqual(len(self.client.cards), 1)

    def test_help_reply_not_in_thread(self):
        self.handle("@_user_1 你好")
        self.assertIn("没看懂", self.client.replies[0][1])
        self.assertFalse(self.client.replies[0][2])

    def test_reserve_from_card_records_operator(self):
        hltv_list = unittest.mock.patch.object(self.app.stages.hltv, "list_matches",
                                               unittest.mock.AsyncMock(return_value=MATCHES))
        fill = unittest.mock.patch.object(self.app, "_fill_official_stream", unittest.mock.AsyncMock())

        async def reserve(match_id):
            return await self.app._card_action(
                {"action": "reserve", "match_id": match_id, "view": {"day": S.DAY_ALL}}, {}, "ou_me", "om_schedule")
        clock = unittest.mock.patch.object(self.app.time, "time", return_value=NOW)
        with hltv_list, fill, clock:
            message = asyncio.run(reserve(1))
            self.assertIn("已预约 Vitality vs MOUZ", message)
            self.assertIn("还没有直播链接", message)
            monitors = db.list_monitors()
            self.assertEqual([m["event_name"] for m in monitors], ["IEM Chengdu"])
            window = monitors[0]["windows"][0]
            self.assertEqual((window["match_id"], window["reserved_by"]), (1, "ou_me"))
            self.assertEqual(window["start"], MATCHES[0]["unix_ms"] / 1000 - 15 * 60)
            self.assertEqual(self.client.updates[0][0], "om_schedule")
            # 同一赛事的另一场：加到同一个直播间
            asyncio.run(reserve(2))
            self.assertEqual(len(db.list_monitors()), 1)
            self.assertEqual(len(db.list_monitors()[0]["windows"]), 2)
            with self.assertRaises(self.app.HTTPException):
                asyncio.run(reserve(1))
            message = asyncio.run(self.app._card_action(
                {"action": "unreserve", "match_id": 1, "view": {}}, {}, "ou_me", "om_schedule"))
            self.assertIn("已取消预约", message)
            self.assertEqual([w["match_id"] for w in db.list_monitors()[0]["windows"]], [2])
        # 预约不生成任务
        self.assertEqual(db.list_tasks(), [])


if __name__ == "__main__":
    unittest.main()
