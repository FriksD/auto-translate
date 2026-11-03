"""HLTV 解析与背景信息组装的测试，样本裁剪自 2026-10 的真实页面。"""
import asyncio
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.pipeline import context as context_builder
from backend.pipeline.prompts import format_background
from backend.sources import hltv_parse as P

FIXTURES = Path(__file__).parent / "fixtures" / "hltv"


def fixture(name: str) -> str:
    return (FIXTURES / f"{name}.html").read_text(encoding="utf-8")


class ParseMatchesTest(unittest.TestCase):
    def test_parses_upcoming_and_live_matches(self):
        matches = P.parse_matches(fixture("matches"))
        self.assertEqual(len({m["id"] for m in matches}), len(matches))  # 置顶区重复的比赛已去重
        live = [m for m in matches if m["live"]]
        self.assertTrue(live and live[0]["unix_ms"] is None)
        semi = next(m for m in matches if m["id"] == 2399756)
        self.assertEqual(semi["team1"], {"id": 7969, "name": "Nemiga"})
        self.assertEqual(semi["team2"], {"id": 9565, "name": "Vitality"})
        self.assertEqual(semi["event"]["name"], "PGL Masters Bucharest 2026")
        self.assertEqual((semi["stars"], semi["format"], semi["unix_ms"]), (5, "bo3", 1793361600000))


class ParseMatchTest(unittest.TestCase):
    def test_finished_match_has_lineups_maps_and_veto(self):
        match = P.parse_match(fixture("match_finished"))
        self.assertEqual((match["team1"]["name"], match["team1"]["score"]), ("Falcons", 2))
        self.assertEqual((match["team2"]["name"], match["team2"]["score"]), ("Vitality", 3))
        self.assertEqual(match["status"], "over")
        self.assertEqual(match["event"]["name"], "IEM Melbourne 2025")
        self.assertEqual([p["name"] for p in match["team1"]["players"]], ["m0NESY", "Magisk", "kyxsan", "NiKo", "TeSeS"])
        self.assertEqual(match["team1"]["players"][0]["real_name"], "Ilya Osipov")
        self.assertEqual(match["maps"][4], {"name": "Nuke", "team1_score": 20, "team2_score": 22, "picked_by": None, "status": "finished", "winner": 2})
        self.assertEqual(match["maps"][1]["picked_by"], "Falcons")
        self.assertEqual(match["veto"][-1], "7. Nuke was left over")
        self.assertEqual(match["head_to_head"], {"team1_wins": 0, "overtimes": 1, "team2_wins": 2})
        self.assertEqual(match["past_matches"]["team1"][3]["opponent"], "Natus Vincere")

    def test_upcoming_match_has_standins_and_streams(self):
        match = P.parse_match(fixture("match_upcoming"))
        self.assertEqual(match["status"], "upcoming")
        jl = match["team2"]["players"][-1]
        self.assertEqual((jl["name"], jl["standin_for"], jl["nationality"]), ("jL", "apEX", "Lithuania"))
        self.assertEqual((match["team1"]["hltv_rank"], match["team1"]["vrs_rank"]), (21, 13))
        pgl = match["streams"][0]
        self.assertEqual((pgl["provider"], pgl["name"], pgl["url"]), ("twitch", "PGL", "https://www.twitch.tv/pgl"))

    def test_tbd_match_has_no_teams(self):
        match = P.parse_match(fixture("match_tbd"))
        self.assertEqual((match["team1"]["name"], match["team2"]["name"]), ("", ""))
        self.assertEqual(match["format"], "Best of 5 (LAN) * Grand final")


class MapWidgetTest(unittest.TestCase):
    def test_map_widgets_with_percent_are_not_maps(self):
        # 实测 Aurora vs G2：页面里带胜率的小组件（"Dust2 - 66%"）也用 .mapholder，排在真实地图前面，
        # 被当成「正在打的图」，真正的当前图成了未开始，读不到回合比分
        import copy
        from bs4 import BeautifulSoup
        expected = P.parse_match(fixture("match_live"))["maps"]
        soup = BeautifulSoup(fixture("match_live"), "html.parser")
        first = soup.select_one(".mapholder")
        for label in ("Dust2 - 66%", "Anubis - 68%", "Mirage - 64%"):
            widget = copy.copy(first)
            widget.select_one(".mapname").string = label
            first.insert_before(widget)
        match = P.parse_match(str(soup))
        self.assertEqual(match["maps"], expected)
        self.assertEqual(sum(1 for m in match["maps"] if m["status"] == "live"), 1)
        self.assertFalse(any("%" in m["name"] for m in match["maps"]))


class ParseTeamTest(unittest.TestCase):
    def test_team_page(self):
        team = P.parse_team(fixture("team_vitality"))
        self.assertEqual(team["coach"], {"nick": "XTQZZZ", "real_name": "Rémy Quoniam", "full": "Rémy 'XTQZZZ' Quoniam"})
        self.assertEqual((team["hltv_rank"], team["vrs_rank"]), (2, 1))
        self.assertEqual([p["name"] for p in team["roster"]], ["apEX", "ZywOo", "flameZ", "mezii", "ropz"])
        self.assertEqual(team["roster"][1]["id"], 11893)
        dust2 = team["map_stats"][0]
        self.assertEqual((dust2["map"], dust2["win_pct"], dust2["wins"], dust2["losses"], dust2["pick_pct"]),
                         ("Dust2", "82.4%", 14, 3, 51))
        self.assertEqual(team["recent_results"][0]["opponent"], "Natus Vincere")
        self.assertTrue(team["news"][0]["title"].startswith("Vitality march into"))

    def test_team_photos(self):
        photos = P.parse_team(fixture("team_vitality"))["photos"]
        self.assertEqual([p["id"] for p in photos], [281972, 281971])
        self.assertIn("w=1000", photos[0]["url"])  # 取 srcset 里最大的一张，不要 avif
        self.assertNotIn("fm=avif", photos[0]["url"])

    def test_player_page(self):
        player = P.parse_player(fixture("player_brollan"))
        self.assertTrue(player["bodyshot"].startswith("https://img-cdn.hltv.org/playerbodyshot/"))
        self.assertEqual(len(player["pictures"]), 3)
        self.assertEqual(player["pictures"][0]["page"], "https://www.hltv.org/gallery/view/282270")
        self.assertIn("w=800", player["pictures"][0]["url"])

    def test_search(self):
        self.assertEqual(P.parse_search_team(fixture("search_falcons"))["id"], 11283)

    def test_helpers(self):
        self.assertEqual(P.split_full_name("Danny 'zonic' Sørensen"), {"nick": "zonic", "real_name": "Danny Sørensen"})
        self.assertEqual(P.match_id_from_url("https://www.hltv.org/matches/2381773/x"), 2381773)


class FakeClient:
    def __init__(self, match_fixture, match_time_ms=None):
        self.match = P.parse_match(fixture(match_fixture))
        if match_time_ms:
            self.match["unix_ms"] = match_time_ms
        self.team_requests = []

    async def get_match(self, match_id, refresh=False):
        return {**self.match, "id": match_id, "url": "https://www.hltv.org/matches/1/x"}

    async def get_team(self, team_id, refresh=False):
        self.team_requests.append(team_id)
        return {**P.parse_team(fixture("team_vitality")), "id": team_id}


class BuildContextTest(unittest.TestCase):
    def test_recent_match_uses_team_pages(self):
        client = FakeClient("match_upcoming", match_time_ms=time.time() * 1000 + 3600_000)
        context, warnings = asyncio.run(context_builder.build_from_match(client, 2399756))
        self.assertEqual(warnings, [])
        self.assertEqual(client.team_requests, [7969, 9565])
        vitality = context["teams"][1]
        self.assertEqual(vitality["players"], ["ropz", "ZywOo", "flameZ", "mezii", "jL"])
        self.assertEqual(vitality["stats"]["coach"], "XTQZZZ")
        self.assertTrue(vitality["map_stats"])
        self.assertIn("jL 替补 apEX", format_background(context))

    def test_old_match_keeps_lineup_but_skips_current_team_data(self):
        client = FakeClient("match_finished")
        context, warnings = asyncio.run(context_builder.build_from_match(client, 2381773))
        self.assertEqual(client.team_requests, [])
        self.assertEqual(len(warnings), 1)
        self.assertEqual(context["teams"][0]["players"], ["m0NESY", "Magisk", "kyxsan", "NiKo", "TeSeS"])
        background = format_background(context)
        self.assertIn("最终比分：Falcons 2-3 Vitality", background)
        self.assertIn("2025-04-27 14:30（北京时间）", background)

    def test_tbd_teams_are_skipped_with_warning(self):
        context, warnings = asyncio.run(context_builder.build_from_match(FakeClient("match_tbd"), 2399759))
        self.assertEqual(context["teams"], [])
        self.assertEqual(len(warnings), 2)


if __name__ == "__main__":
    unittest.main()
