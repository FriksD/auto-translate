"""在线精校：公网访问边界、飞书登录会话、段落编辑与冲突、音频对齐、接口的测试。"""
import json
import sys
import tempfile
import time
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient

from backend import config, db, review, web_auth
from backend.integrations import task_cards
from backend.runner import State

PUBLIC = "https://demo.ngrok-free.dev"


class BoundaryTest(unittest.TestCase):
    def test_is_public(self):
        self.assertFalse(web_auth.is_public({"host": "127.0.0.1:8000"}))
        self.assertFalse(web_auth.is_public({"host": "localhost:5173"}))
        self.assertFalse(web_auth.is_public({"host": "[::1]:8000"}))
        self.assertTrue(web_auth.is_public({"host": "demo.ngrok-free.dev"}))
        # ngrok 用 --host-header=rewrite 时 Host 是本机，但仍带 X-Forwarded-For
        self.assertTrue(web_auth.is_public({"host": "127.0.0.1:8000", "x-forwarded-for": "1.2.3.4"}))

    def test_paths(self):
        for path in ("/review/3", "/assets/index.js", "/api/review/3", "/auth/feishu/login"):
            self.assertTrue(web_auth.public_path_allowed(path), path)
        for path in ("/", "/api/settings", "/api/tasks/3", "/settings", "/api/reviewx"):
            self.assertFalse(web_auth.public_path_allowed(path), path)

    def test_safe_next(self):
        self.assertEqual(web_auth.safe_next("/review/3"), "/review/3")
        self.assertEqual(web_auth.safe_next("https://evil.com"), "/review/")
        self.assertEqual(web_auth.safe_next("/review//evil.com"), "/review/")


class SessionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        config.use_data_dir(Path(self.tmp.name))
        web_auth._secret = None

    def tearDown(self):
        web_auth._secret = None
        self.tmp.cleanup()

    def test_roundtrip_tamper_expiry(self):
        cookie = web_auth.make_session({"open_id": "ou_a", "name": "小王"}, now=1000)
        self.assertEqual(web_auth.read_session(cookie, now=2000)["name"], "小王")
        self.assertIsNone(web_auth.read_session(cookie, now=1000 + 31 * 86400))
        payload, signature = cookie.rsplit(".", 1)
        forged = web_auth.make_session({"open_id": "ou_b", "name": "x"}, now=1000).rsplit(".", 1)[0]
        self.assertIsNone(web_auth.read_session(f"{forged}.{signature}", now=2000))
        self.assertIsNone(web_auth.read_session("garbage", now=2000))


class ParagraphTest(unittest.TestCase):
    PARAS = ["Q：一", "A：二", "Q：三"]

    def test_edit(self):
        paragraphs, en_index, new = review.apply_edit(self.PARAS, [0, 1, 2], 1, "A：二", "A：二\n改")
        self.assertEqual(paragraphs, ["Q：一", "A：二改", "Q：三"])
        self.assertEqual(new, "A：二改")

    def test_shifted_by_delete(self):
        # 别人删了第一段，第三段跑到了下标 1
        paragraphs, en_index, _ = review.apply_edit(["A：二", "Q：三"], [1, 2], 2, "Q：三", "Q：三！")
        self.assertEqual(paragraphs, ["A：二", "Q：三！"])
        self.assertEqual(en_index, [1, 2])

    def test_conflict(self):
        with self.assertRaises(review.Conflict):
            review.apply_edit(["Q：一", "A：别人改的", "Q：三"], [0, 1, 2], 1, "A：二", "A：我改的")

    def test_delete_keeps_english_pairing(self):
        paragraphs, en_index, _ = review.apply_edit(self.PARAS, [0, 1, 2], 0, "Q：一", "  ")
        self.assertEqual((paragraphs, en_index), (["A：二", "Q：三"], [1, 2]))

    def test_split_answer_pairs_by_qa_block(self):
        # 译稿把第一个回答拆成了两段（第二段没有 A：前缀），英文稿则把第二个问题拆成两段
        zh = ["Q：一", "A：二", "二续", "Q：三", "A：四"]
        en = ["Q: one", "A: two", "Q: three", "three more", "A: four"]
        self.assertEqual(review.qa_blocks(zh), [0, 1, 1, 2, 3])
        self.assertEqual(review.group_blocks(en), ["Q: one", "A: two", "Q: three three more", "A: four"])

    def test_paragraph_times(self):
        transcript = {"segments": [
            {"start": 0.0, "end": 3.0, "text": "Thanks for watching!"},
            {"start": 10.0, "end": 14.0, "text": "How happy are you today?"},
            {"start": 15.0, "end": 20.0, "text": "Very happy, we played well."},
            {"start": 30.0, "end": 31.0, "words": [{"start": 30.2, "end": 30.6, "word": " Last"},
                                                   {"start": 30.6, "end": 31.0, "word": " question."}],
             "text": "Last question."},
        ]}
        english = ["Q: How happy are you today?", "A: Very happy. We played well.", "Q: Last question.",
                   "A: Something nobody said"]
        self.assertEqual(review.paragraph_times(english, transcript), [10.0, 15.0, 30.2, 30.2])


class ReviewApiTest(unittest.TestCase):
    def setUp(self):
        from backend import app as app_module

        self.tmp = tempfile.TemporaryDirectory()
        config.use_data_dir(Path(self.tmp.name))
        web_auth._secret = None
        db.init_db()
        db.save_settings({"feishu": {"enabled": False, "app_id": "a", "app_secret": "s",
                                     "receive_id_type": "chat_id", "receive_id": "oc_group", "public_url": PUBLIC}})
        self.task = db.create_task(title="A vs B", state=State.REVIEW, source_type="live")
        directory = config.task_dir(self.task["id"])
        (directory / "draft_zh.txt").write_text("Q：你好吗？\n\nA：很好。\n", encoding="utf-8")
        (directory / "clean_en.txt").write_text("Q: How are you?\n\nA: Good.\n", encoding="utf-8")
        (directory / "transcript.json").write_text(json.dumps({"duration": 20.0, "segments": [
            {"start": 2.0, "end": 4.0, "text": "How are you?"}, {"start": 5.0, "end": 6.0, "text": "Good."}]}),
            encoding="utf-8")
        # 录制从 t0 开始，采访在录音的 100～115 秒，截取时前后留 10 秒
        t0 = time.mktime((2026, 10, 31, 1, 0, 0, 0, 0, -1))
        (directory / "live.json").write_text(json.dumps({
            "recording": {"started_at": t0}, "interview": {"start": 100.0, "end": 115.0, "clip_start": 90.0}}))
        frames = directory / "frames"
        frames.mkdir()
        for seconds in (80, 92, 97, 200):
            name = time.strftime("f_%Y%m%d_%H%M%S.jpg", time.localtime(t0 + seconds))
            (frames / name).write_bytes(b"jpg")
        self.local = TestClient(app_module.app, base_url="http://127.0.0.1:8000")
        self.public = TestClient(app_module.app, base_url=PUBLIC)
        patcher = unittest.mock.patch.object(web_auth.members, "contains", side_effect=lambda _, oid: oid == "ou_in")
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        web_auth._secret = None
        db._conn.close()
        self.tmp.cleanup()

    def url(self, suffix=""):
        return f"/api/review/{self.task['id']}{suffix}"

    def cookie(self, open_id):
        return {"cookie": f"{web_auth.SESSION_COOKIE}={web_auth.make_session({'open_id': open_id, 'name': '小王'})}"}

    def test_public_cannot_reach_other_api(self):
        self.assertEqual(self.public.get("/api/settings").status_code, 404)
        self.assertEqual(self.public.get(f"/api/tasks/{self.task['id']}").status_code, 404)
        self.assertEqual(self.local.get("/api/settings").status_code, 200)

    def test_login_required_and_group_only(self):
        self.assertEqual(self.public.get(self.url()).status_code, 401)
        self.assertEqual(self.public.get(self.url(), headers=self.cookie("ou_out")).status_code, 403)
        response = self.public.get(self.url(), headers=self.cookie("ou_in"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["user"]["name"], "小王")

    def test_login_redirect(self):
        response = self.public.get("/auth/feishu/login?next=/review/1", follow_redirects=False)
        self.assertEqual(response.status_code, 307)
        location = response.headers["location"]
        self.assertTrue(location.startswith(web_auth.AUTHORIZE_URL))
        self.assertIn("redirect_uri=https%3A%2F%2Fdemo.ngrok-free.dev%2Fauth%2Ffeishu%2Fcallback", location)
        self.assertIn("|/review/1", response.headers["set-cookie"])

    def test_payload_frames_and_times(self):
        data = self.local.get(self.url()).json()
        self.assertEqual([p["en"] for p in data["paragraphs"]], ["Q: How are you?", "A: Good."])
        self.assertEqual([p["start"] for p in data["paragraphs"]], [2.0, 5.0])
        # 采访音频从录音第 90 秒开始：第 80 秒、第 200 秒的画面不在范围内
        self.assertEqual([f["t"] for f in data["frames"]], [2.0, 7.0])
        name = data["frames"][0]["name"]
        self.assertEqual(self.local.get(self.url(f"/frames/{name}")).content, b"jpg")
        self.assertEqual(self.local.get(self.url("/frames/live.json")).status_code, 404)
        self.assertNotIn(b"recording", self.local.get(self.url("/frames/..%2Flive.json")).content)

    def test_edit_conflict_and_finalize(self):
        response = self.public.put(self.url("/paragraphs/1"), json={"base": "A：很好。", "text": "A：挺好的。"},
                                   headers=self.cookie("ou_in"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["paragraphs"][1]["edited"]["by"], "小王")
        draft = (config.task_dir(self.task["id"]) / "draft_zh.txt").read_text(encoding="utf-8")
        self.assertEqual(draft, "Q：你好吗？\n\nA：挺好的。\n")
        # 另一个人还拿着旧内容在改
        conflict = self.local.put(self.url("/paragraphs/1"), json={"base": "A：很好。", "text": "A：还行。"})
        self.assertEqual(conflict.status_code, 409)

        result = self.local.post(self.url("/finalize")).json()
        self.assertEqual(result["task"]["state"], State.DONE)
        self.assertEqual(result["finalized"]["by"], "本机")
        self.assertIsNotNone(next(config.task_dir(self.task["id"]).glob("*.docx"), None))

    def test_payload_split_answer_stays_aligned(self):
        directory = config.task_dir(self.task["id"])
        draft = directory / "draft_zh.txt"
        draft.write_text("Q：你好吗？\n\nA：很好。\n\n真的很好。\n", encoding="utf-8")
        # 旧版本按段落序号存的对应关系（MOUZ vs Vitality 那场）要按问答块重算
        (directory / "review.json").write_text(json.dumps({"en_index": [0, 1, 2], "draft_mtime": draft.stat().st_mtime}))
        paragraphs = self.local.get(self.url()).json()["paragraphs"]
        self.assertEqual([p["en"] for p in paragraphs], ["Q: How are you?", "A: Good.", ""])
        self.assertEqual([p["start"] for p in paragraphs], [2.0, 5.0, None])
        # 删掉回答的第一段，续段接上英文
        response = self.local.put(self.url("/paragraphs/1"), json={"base": "A：很好。", "text": ""})
        self.assertEqual([p["en"] for p in response.json()["paragraphs"]], ["Q: How are you?", "A: Good."])

    def test_busy_task_not_editable(self):
        db.update_task(self.task["id"], state=State.PROCESSING)
        response = self.local.put(self.url("/paragraphs/0"), json={"base": "Q：你好吗？", "text": "Q：好吗"})
        self.assertEqual(response.status_code, 409)

    def test_card_has_review_button(self):
        card = task_cards.render(db.get_task(self.task["id"]))
        urls = [b["behaviors"][0].get("default_url") for col in card["body"]["elements"][-1]["columns"]
                for b in col["elements"]]
        self.assertIn(f"{PUBLIC}/review/{self.task['id']}", urls)


if __name__ == "__main__":
    unittest.main()
