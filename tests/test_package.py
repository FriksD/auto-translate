"""投稿信息（推荐标题、摘要、封面）的测试。"""
import json
import sys
import tempfile
import time
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend import config, db
from backend.pipeline import package, prompts
from backend.runner import State


def make_context():
    return {
        "match": {"event": "PGL Masters Bucharest 2026", "format": "Best of 3 (LAN) * 3rd place decider match",
                  "unix_ms": 1793361600000, "score": [None, None], "maps": []},
        "teams": [
            {"id": 4494, "name": "MOUZ", "players": ["xertioN"], "player_details": [{"id": 18072, "name": "xertioN"}]},
            {"id": 9565, "name": "Vitality", "players": ["ZywOo"], "player_details": [{"name": "ZywOo"}]},
        ],
    }


def make_task(**extra):
    task = {"id": 1, "title": "MOUZ vs Vitality", "source_type": "live", "team1": "MOUZ", "team2": "Vitality",
            "match_time": 1793361600, "context": make_context()}
    task.update(extra)
    return task


LIVE_STATE = {
    "snapshot": {"status": "over", "map_wins": [2, 0], "maps": [
        {"name": "Mirage", "team1_score": 13, "team2_score": 9, "status": "finished"},
        {"name": "Nuke", "team1_score": 13, "team2_score": 11, "status": "finished"},
        {"name": "Inferno", "team1_score": None, "team2_score": None, "status": "upcoming"},
    ]},
    "match_over_at": 1000.0,
    "detection": {"interviewee": "xertioN"},
}


class FactsTest(unittest.TestCase):
    def test_live_task_uses_final_snapshot(self):
        facts = package.match_facts(make_task(), LIVE_STATE, now=2000.0)
        self.assertEqual((facts["winner"], facts["loser"], facts["score"]), ("MOUZ", "Vitality", "2-0"))
        self.assertEqual((facts["stage"], facts["best_of"]), ("3rd place decider match", 3))
        self.assertEqual(facts["maps"], ["Mirage 13-9", "Nuke 13-11"])
        self.assertEqual(facts["date"], "2026-10-30")
        self.assertTrue(facts["just_ended"])
        self.assertEqual(facts["interviewee_guess"], "xertioN")

    def test_winner_can_be_second_team(self):
        task = make_task(source_type="url")
        task["context"]["match"]["score"] = [1, 2]
        facts = package.match_facts(task, {})
        self.assertEqual((facts["winner"], facts["loser"], facts["score"]), ("Vitality", "MOUZ", "2-1"))
        self.assertFalse(facts["just_ended"])

    def test_unknown_score(self):
        facts = package.match_facts(make_task(), {})
        self.assertIsNone(facts["winner"])
        self.assertIn("比分未知", prompts._format_facts(facts))


class HelpersTest(unittest.TestCase):
    def test_event_terms_longest_first(self):
        terms = {"PGL": "PGL", "PGL Masters Bucharest": "PGL布加勒斯特大师赛", "IEM": "IEM"}
        self.assertEqual(package.event_terms("PGL Masters Bucharest 2026", terms),
                         [("PGL Masters Bucharest", "PGL布加勒斯特大师赛"), ("PGL", "PGL")])

    def test_host_for(self):
        lines = ["PGL=BanKs", "IEM = Tres", "Sadokist", ""]
        self.assertEqual(package.host_for("PGL Masters Bucharest 2026", lines), ("BanKs", ["BanKs", "Tres", "Sadokist"]))
        self.assertIsNone(package.host_for("BLAST Open", lines)[0])

    def test_apply_answer_cleans_titles_and_checks_summary(self):
        facts = package.match_facts(make_task(), LIVE_STATE, now=2000.0)
        fields, warnings = package.apply_answer({
            "titles": ["1. 「xertioN赛后采访：我们依然想击败他们」", "xertioN赛后采访：我们依然想击败他们", "", "- 第二个"],
            "summary": "PGL布加勒斯特大师赛-2026今日正式落下帷幕，MOUZ以2-0的比分击败Vitality；"
                       "比赛结束后，xertioN接受了BanKs的采访。",
            "interviewee": "xertioN",
        }, facts)
        self.assertEqual(fields["titles"], ["xertioN赛后采访：我们依然想击败他们", "第二个"])
        self.assertEqual(warnings, [])
        _, warnings = package.apply_answer({"titles": ["x"], "summary": "MOUZ以2-1的比分击败Vitality"}, facts)
        self.assertEqual(len(warnings), 1)
        self.assertIn("2-0", warnings[0])

    def test_pick_evenly(self):
        self.assertEqual(package.pick_evenly(list(range(10)), 4), [0, 3, 6, 9])
        self.assertEqual(package.pick_evenly([1, 2], 4), [1, 2])

    def test_prompt_mentions_facts_examples_and_host(self):
        facts = package.match_facts(make_task(), LIVE_STATE, now=2000.0)
        prompt = prompts.build_package_prompt(facts, "Q：问\n\nA：答", ["标题甲"], ["摘要乙"],
                                              [("PGL Masters Bucharest", "PGL布加勒斯特大师赛")], "BanKs", ["BanKs"])
        for text in ("标题甲", "摘要乙", "PGL布加勒斯特大师赛", "BanKs", "MOUZ 2-0 击败 Vitality", "就在刚刚", "A：答"):
            self.assertIn(text, prompt)


class StoredTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        config.use_data_dir(Path(self.tmp.name))
        db.init_db()

    def tearDown(self):
        db._conn.close()
        self.tmp.cleanup()

    def test_remember_examples_puts_newest_first(self):
        package.remember_examples("新标题", "新摘要")
        package.remember_examples("新标题", "新摘要")  # 重复定稿不重复添加
        cfg = db.get_settings()["package"]
        self.assertEqual(cfg["title_examples"][0], "新标题")
        self.assertEqual(cfg["title_examples"].count("新标题"), 1)
        self.assertEqual(cfg["summary_examples"][0], "新摘要")

    def test_update_and_chosen_title(self):
        package.save(3, {"titles": ["甲", "乙"], "summary": "摘要"})
        self.assertEqual(package.chosen_title(package.load(3)), "甲")
        package.update(3, title="乙 ", summary="改过的\n摘要")
        data = package.load(3)
        self.assertEqual((package.chosen_title(data), data["summary"]), ("乙", "改过的 摘要"))

    def test_generate_keeps_text_when_covers_fail_and_sends_copyable_messages(self):
        task = db.create_task(title="MOUZ vs Vitality", state=State.REVIEW, source_type="url",
                              team1="MOUZ", team2="Vitality", context=make_context())
        (config.task_dir(task["id"]) / "draft_zh.txt").write_text("Q：问\n\nA：答\n", encoding="utf-8")
        answer = json.dumps({"titles": ["xertioN赛后采访：甲", "xertioN赛后采访：乙"], "summary": "摘要",
                             "interviewee": "xertioN", "interviewee_team": "MOUZ"})
        with unittest.mock.patch("backend.pipeline.stages._llm_call", return_value=answer), \
                unittest.mock.patch.object(package, "_video_stills", side_effect=RuntimeError("没有视频")), \
                unittest.mock.patch.object(package, "_hltv_pictures", return_value=[]):
            result = package.generate(task["id"])
        self.assertEqual(result["status"], "done")
        self.assertEqual(result["titles"][0], "xertioN赛后采访：甲")
        self.assertTrue(any("截取采访画面失败" in w for w in result["warnings"]))

        sent = []
        settings = db.get_settings()
        settings["feishu"]["enabled"] = True
        db.save_settings(settings)
        with unittest.mock.patch.object(package.cards, "alert", side_effect=lambda *a, **k: sent.append(("alert", a[1]))), \
                unittest.mock.patch.object(package.cards, "send_text", side_effect=lambda _, t: sent.append(("text", t))):
            package.send_to_feishu(task["id"], result)
        self.assertEqual(sent[0], ("alert", "投稿信息"))
        self.assertEqual(sent[1:], [("text", "xertioN赛后采访：甲"), ("text", "摘要"),
                                    ("text", "其他候选标题：\n2. xertioN赛后采访：乙")])


if __name__ == "__main__":
    unittest.main()
