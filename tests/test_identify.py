"""直播间截下的采访：事后识别受访者、战队、对应比赛，以及摘要按采访类型写的测试。"""
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
from backend.pipeline import identify, package, prompts

AT = time.mktime(time.strptime("2026-11-08 15:00:00", "%Y-%m-%d %H:%M:%S"))


def iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(ts))


def bo3_row(team1, team2, start, status="finished", slug=None):
    return {"slug": slug or f"{team1}-vs-{team2}".lower(), "status": status, "start_date": iso(start),
            "team1": {"name": team1, "slug": team1.lower()}, "team2": {"name": team2, "slug": team2.lower()}}


class PureTest(unittest.TestCase):
    def test_event_teams(self):
        matches = [
            {"event": {"id": 8000, "name": "IEM Beijing 2026"}, "team1": {"name": "Vitality"}, "team2": {"name": "TBD"}},
            {"event": {"id": 8001, "name": "CCT Europe"}, "team1": {"name": "Sashi"}, "team2": {"name": "Rebels"}},
            {"event": {"id": 8000, "name": "IEM Beijing 2026"}, "team1": {"name": "MOUZ"}, "team2": {"name": "FaZe"}},
        ]
        self.assertEqual(identify.event_teams(matches, "IEM Beijing", None), ["Vitality", "MOUZ", "FaZe"])
        self.assertEqual(identify.event_teams(matches, "", "8001"), ["Sashi", "Rebels"])

    def test_nearby_and_unique(self):
        rows = [bo3_row("Vitality", "MOUZ", AT - 3600), bo3_row("Old", "Older", AT - 10 * 3600)]
        self.assertEqual(identify.nearby_teams(rows, AT), ["Vitality", "MOUZ"])
        self.assertEqual(identify.unique(["Team Vitality", "Vitality", "MOUZ"]), ["Team Vitality", "MOUZ"])

    def test_event_names_and_bo3_candidates(self):
        self.assertTrue(identify.same_event_name("IEM Beijing 2026", "Intel Extreme Masters Beijing 2026"))
        self.assertFalse(identify.same_event_name("IEM Beijing 2026", "Intel Extreme Masters Cologne 2026"))
        self.assertFalse(identify.same_event_name("IEM北京", "Intel Extreme Masters Beijing 2026"))
        iem = {"name": "Intel Extreme Masters Beijing 2026"}
        rows = [{**bo3_row("MOUZ", "BetBoom", AT - 20 * 3600), "tournament": iem, "tier": "s"},
                {**bo3_row("Sashi", "Rebels", AT - 600), "tournament": {"name": "CCT Europe"}, "tier": "c"},
                {**bo3_row("FURIA", "PARIVISION", AT - 600), "tournament": {"name": "Other S"}, "tier": "s"}]
        # 赛事名对得上：这个赛事所有比赛的队伍，不限时间
        self.assertEqual(identify.bo3_event_teams(rows, "IEM Beijing 2026", AT), ["MOUZ", "BetBoom"])
        # 对不上：附近的 S / A 级比赛
        self.assertEqual(identify.bo3_event_teams(rows, "IEM北京", AT), ["FURIA", "PARIVISION"])

    def test_parse_identity_aligns_names(self):
        parsed = identify.parse_identity({"interviewees": "ZywOo", "team": "team vitality", "opponent": "Mouz",
                                          "about_match": "Just_Played", "result": "won"}, ["Vitality", "MOUZ"])
        self.assertEqual((parsed["team"], parsed["opponent"], parsed["about_match"], parsed["result"]),
                         ("Vitality", "MOUZ", "just_played", "won"))
        self.assertEqual(parsed["interviewees"], ["ZywOo"])
        parsed = identify.parse_identity({"team": "Vitality", "opponent": "Vitality", "about_match": "later"}, [])
        self.assertIsNone(parsed["opponent"])
        self.assertEqual(parsed["about_match"], "unknown")

    def test_pick_match(self):
        rows = [bo3_row("MOUZ", "Vitality", AT - 2 * 3600, slug="a"),
                bo3_row("Vitality", "FaZe", AT - 6 * 3600, slug="b"),
                bo3_row("Vitality", "NAVI", AT - 600, status="current", slug="c"),
                bo3_row("Vitality", "G2", AT + 3600, status="upcoming", slug="d")]
        found = identify.pick_match(rows, "Vitality", "MOUZ", "just_played", AT)
        self.assertEqual((found[0]["slug"], found[1]), ("a", True))
        # 没说对手、刚打完的候选有两场：不确认
        self.assertIsNone(identify.pick_match(rows, "Vitality", None, "just_played", AT))
        self.assertEqual(identify.pick_match(rows, "Vitality", None, "between_maps", AT)[0]["slug"], "c")
        self.assertIsNone(identify.pick_match(rows, "Vitality", "G2", "upcoming", AT))
        self.assertIsNone(identify.pick_match(rows, "Vitality", "Spirit", "just_played", AT))

    def test_context_match(self):
        normalized = {"status": "over", "best_of": 3, "event": "IEM Beijing 2026", "start_time": AT - 7200,
                      "team1": {"name": "Vitality", "score": 2}, "team2": {"name": "MOUZ", "score": 1},
                      "maps": [{"name": "Mirage", "team1_score": 13, "team2_score": 8, "status": "finished",
                                "winner": 1, "began_at": 1}], "url": "u"}
        match = identify.context_match(normalized, "fallback")
        self.assertEqual((match["score"], match["format"], match["event"]), ([2, 1], "Best of 3", "IEM Beijing 2026"))
        self.assertEqual(match["maps"][0], {"name": "Mirage", "team1_score": 13, "team2_score": 8,
                                            "status": "finished", "winner": 1})
        self.assertIsNone(identify.context_match({**normalized, "status": "live"}, "x")["score"])


class FactsTest(unittest.TestCase):
    def _task(self, kind, score=None, opponent="MOUZ"):
        teams = [{"name": "Vitality"}, {"name": opponent}] if opponent else [{"name": "Vitality"}]
        return {"team1": "Vitality", "team2": opponent, "context": {
            "teams": teams, "match": {"event": "IEM Beijing 2026", "format": "Best of 3", "score": score},
            "identified": {"about_match": kind, "interviewees": ["ZywOo"], "team": "Vitality", "opponent": opponent}}}

    def test_post_match_with_confirmed_score(self):
        state = {"recording": {"started_at": AT}, "interview": {"start": 0, "end": 300}}
        facts = package.match_facts(self._task("just_played", [2, 1]), state, now=AT + 600)
        self.assertEqual((facts["winner"], facts["score"], facts["interview_kind"]), ("Vitality", "2-1", "just_played"))
        self.assertTrue(facts["just_ended"])
        self.assertEqual(facts["interviewee_guess"], "ZywOo")
        label, rule = prompts.interview_kind(facts)
        self.assertEqual((label, rule), ("赛后采访", None))

    def test_unconfirmed_and_other_kinds_have_no_score(self):
        state = {"recording": {"started_at": AT}, "interview": {"start": 0, "end": 300}}
        facts = package.match_facts(self._task("just_played"), state, now=AT + 600)
        self.assertIsNone(facts["score"])
        self.assertFalse(facts["just_ended"])
        self.assertIn("比分未知", prompts.interview_kind(facts)[1])
        facts = package.match_facts(self._task("upcoming"), state, now=AT + 600)
        label, rule = prompts.interview_kind(facts)
        self.assertEqual(label, "赛前采访")
        self.assertIn("Vitality对阵MOUZ", rule)
        facts = package.match_facts(self._task("general", opponent=None), state, now=AT + 600)
        prompt = prompts.build_package_prompt(facts, "Q：问\n\nA：答", [], [], [], None, [])
        self.assertIn("CS2 选手采访", prompt)
        self.assertIn("「受访选手采访：核心观点」", prompt)
        self.assertIn("不是针对某一场比赛", prompt)

    def test_old_reserved_tasks_unchanged(self):
        task = {"team1": "A", "team2": "B", "context": {"teams": [{"name": "A"}, {"name": "B"}],
                                                        "match": {"score": [2, 0]}}}
        facts = package.match_facts(task, {"match_over_at": AT}, now=AT + 60)
        self.assertEqual(facts["interview_kind"], "just_played")
        self.assertTrue(facts["just_ended"])
        self.assertIn("赛后采访", prompts.build_package_prompt(facts, "", [], [], [], None, []))


class BuildTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        config.use_data_dir(Path(self.tmp.name))
        db.init_db()
        self.monitor = db.create_monitor(title="IEM 北京", event_name="IEM Beijing 2026", event_id="8000",
                                         streams=[{"url": "https://live.bilibili.com/35", "proxy": False}])
        self.task = db.create_task(title="t", state="draft", source_type="live", team1="Vitality",
                                   context={"monitor": {"id": self.monitor["id"]}})
        workdir = config.task_dir(self.task["id"])
        (workdir / "live.json").write_text(json.dumps({
            "recording": {"started_at": AT - 300}, "interview": {"start": 300, "end": 500},
            "detection": {"interviewees": ["Zywoo"], "team": "Vitality"}}), encoding="utf-8")
        (workdir / "live_transcript.jsonl").write_text(
            json.dumps({"start": 300, "end": 310, "text": "ZywOo, congrats on the win against MOUZ"}) + "\n",
            encoding="utf-8")

    def tearDown(self):
        db._conn.close()
        self.tmp.cleanup()

    def _run(self, answer, rows):
        hltv = unittest.mock.Mock()
        hltv.list_matches = unittest.mock.AsyncMock(return_value=[
            {"event": {"id": 8000, "name": "IEM Beijing 2026"}, "team1": {"name": "Vitality"},
             "team2": {"name": "MOUZ"}}])
        bo3_client = unittest.mock.Mock()
        bo3_client.list_matches = unittest.mock.AsyncMock(side_effect=[[], rows])
        bo3_client.get_match = unittest.mock.AsyncMock(return_value={
            "status": "over", "best_of": 3, "event": "IEM Beijing 2026", "start_time": AT - 7200,
            "team1": {"name": "Vitality", "score": 2}, "team2": {"name": "MOUZ", "score": 1}, "maps": [], "url": "u"})
        built = {"teams": [{"name": "Vitality", "players": ["ZywOo", "apEX"]}, {"name": "MOUZ", "players": ["xertioN"]}]}
        with unittest.mock.patch.object(identify.bo3, "Bo3Client", return_value=bo3_client), \
                unittest.mock.patch("backend.pipeline.stages._llm_call", return_value=json.dumps(answer)) as llm, \
                unittest.mock.patch.object(identify.context_builder, "build_from_team_names",
                                           unittest.mock.AsyncMock(return_value=(built, []))) as teams:
            result = asyncio.run(identify.build_for_interview(db.get_task(self.task["id"]), hltv, []))
        return result, llm, teams, bo3_client

    def test_post_match_confirmed(self):
        answer = {"interviewees": ["ZywOo"], "team": "Vitality", "opponent": "MOUZ", "about_match": "just_played"}
        (context, updates, warnings), llm, teams, bo3_client = self._run(
            answer, [bo3_row("MOUZ", "Vitality", AT - 7200, slug="m")])
        bo3_client.get_match.assert_awaited_once_with("m", True)
        teams.assert_awaited_once()
        self.assertEqual(teams.await_args.args[1], ["Vitality", "MOUZ"])
        self.assertEqual(context["match"]["score"], [2, 1])
        self.assertEqual((updates["team1"], updates["team2"]), ("Vitality", "MOUZ"))
        self.assertEqual(updates["title"], "IEM Beijing 2026 ZywOo (Vitality) 赛后采访")
        self.assertEqual(warnings, [])
        prompt = llm.call_args.args[3]
        self.assertIn("Vitality, MOUZ", prompt)
        self.assertIn("congrats on the win", prompt)

    def test_unconfirmed_match_has_no_score(self):
        answer = {"interviewees": ["ZywOo"], "team": "Vitality", "opponent": "MOUZ", "about_match": "just_played"}
        (context, updates, warnings), _, _, bo3_client = self._run(answer, [])
        bo3_client.get_match.assert_not_awaited()
        self.assertEqual(context["match"], {"event": "IEM Beijing 2026"})
        self.assertTrue(any("没能确认" in w for w in warnings))

    def test_pre_match_and_unknown_player(self):
        answer = {"interviewees": ["Someone"], "team": "Vitality", "opponent": "MOUZ", "about_match": "upcoming"}
        (context, updates, warnings), _, _, bo3_client = self._run(answer, [bo3_row("MOUZ", "Vitality", AT - 7200)])
        bo3_client.get_match.assert_not_awaited()
        self.assertTrue(updates["title"].endswith("赛前采访"))
        self.assertTrue(any("不在识别出的战队阵容里" in w for w in warnings))


if __name__ == "__main__":
    unittest.main()
