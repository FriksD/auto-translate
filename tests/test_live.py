"""比赛监视（决胜图判断、轮询间隔、数据源）、直播源选择、录音时间轴与采访截取的测试。"""
import json
import sys
import tempfile
import time
import unittest
import unittest.mock
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.config import DEFAULT_SETTINGS
from backend.pipeline import live, match_sources, watcher
from backend.pipeline.recorder import SAMPLE_RATE, chunk_start
from backend.sources import bo3
from backend.sources import hltv_parse as P
from backend.sources.browser import is_challenge, is_hltv_page

FIXTURES = Path(__file__).parent / "fixtures" / "hltv"
BO3_FIXTURES = Path(__file__).parent / "fixtures" / "bo3"
CFG = DEFAULT_SETTINGS["live"]


def fixture(name: str) -> str:
    return (FIXTURES / f"{name}.html").read_text(encoding="utf-8")


def bo3_fixture(name: str):
    return json.loads((BO3_FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def make_match(status="live", best_of=3, finished=(), live_score=None, board_map="Mirage"):
    """finished: 已打完各图的胜方 (1/2)；live_score: 当前图 (team1, team2) 比分（来自实时比分板）"""
    maps = [{"name": f"Map{i}", "team1_score": 13, "team2_score": 5, "status": "finished", "winner": w}
            for i, w in enumerate(finished)]
    board = None
    if live_score is not None:
        maps.append({"name": board_map, "team1_score": None, "team2_score": None, "status": "live", "winner": None})
        # 比分板按 CT / T 两边显示，和 team1 / team2 的顺序无关
        board = {"round": sum(live_score) + 1, "map": board_map,
                 "ct": {"team": "MIBR", "score": live_score[1]}, "t": {"team": "fnatic", "score": live_score[0]}}
    return {"status": status, "best_of": best_of, "maps": maps, "scoreboard": board,
            "team1": {"name": "fnatic"}, "team2": {"name": "MIBR"}, "loaded_at": time.time()}


class ParseLiveTest(unittest.TestCase):
    def test_live_page_has_scoreboard_and_map_status(self):
        html = fixture("match_live")
        match = P.parse_match(html)
        self.assertEqual((match["status"], match["best_of"]), ("live", 3))
        self.assertEqual([(m["name"], m["status"], m["winner"]) for m in match["maps"]],
                         [("Ancient", "finished", 1), ("Mirage", "live", None), ("Anubis", "upcoming", None)])
        self.assertEqual(P.parse_scoreboard(html), {
            "round": 4, "map": "Mirage", "ct": {"team": "fnatic", "score": 2}, "t": {"team": "MIBR", "score": 1}})

    def test_finished_page_has_no_scoreboard(self):
        html = fixture("match_over_bo3")
        match = P.parse_match(html)
        self.assertEqual(match["status"], "over")
        self.assertEqual([m["winner"] for m in match["maps"]], [2, 2, None])
        self.assertIsNone(P.parse_scoreboard(html))


class ChallengeTest(unittest.TestCase):
    def test_challenge_page_detected_in_any_language(self):
        page = fixture("challenge")
        self.assertTrue(is_challenge("Just a moment...", page))
        self.assertTrue(is_challenge("请稍候…", page))
        self.assertTrue(is_challenge("", page))  # 只看页面内容
        self.assertFalse(is_hltv_page(page))
        for name in ("matches", "match_live", "match_over_bo3", "team_vitality"):
            self.assertFalse(is_challenge("HLTV.org", fixture(name)), name)
        self.assertTrue(is_hltv_page('<!DOCTYPE html><html lang="en" data-client-country-iso="SG">'))


class AssessTest(unittest.TestCase):
    def test_scoreboard_sides_are_mapped_to_teams(self):
        score = match_sources.live_map_score(make_match(live_score=(9, 4)))
        self.assertEqual((score["team1"], score["team2"], score["source"]), (9, 4, "scoreboard"))

    def test_first_map_of_bo3_is_not_deciding(self):
        result = watcher.assess(make_match(live_score=(12, 3)), CFG)
        self.assertFalse(result["deciding"])
        self.assertFalse(result["should_record"])
        self.assertEqual(result["interval"], CFG["intervals"]["idle"])

    def test_intervals_follow_leader_score_on_deciding_map(self):
        cases = [((5, 3), "idle", False), ((8, 6), "mid", False), ((10, 4), "mid", True),
                 ((11, 9), "late", True), ((12, 12), "late", True)]
        for score, interval, record in cases:
            result = watcher.assess(make_match(finished=(1,), live_score=score), CFG)
            self.assertTrue(result["deciding"])
            self.assertEqual(result["interval"], CFG["intervals"][interval], score)
            self.assertEqual(result["should_record"], record, score)
        self.assertTrue(watcher.assess(make_match(finished=(1,), live_score=(12, 12)), CFG)["overtime"])

    def test_bo1_and_bo5(self):
        self.assertTrue(watcher.assess(make_match(best_of=1, live_score=(10, 2)), CFG)["should_record"])
        self.assertFalse(watcher.assess(make_match(best_of=5, finished=(1, 2), live_score=(10, 2)), CFG)["deciding"])
        self.assertTrue(watcher.assess(make_match(best_of=5, finished=(1, 2, 1), live_score=(10, 2)), CFG)["should_record"])
        # BO5 2:1，落后方在第四张图领先：打完是 2:2，不录
        self.assertFalse(watcher.assess(make_match(best_of=5, finished=(1, 2, 1), live_score=(2, 10)), CFG)["should_record"])
        self.assertTrue(watcher.assess(make_match(best_of=5, finished=(1, 2, 1, 2), live_score=(2, 10)), CFG)["should_record"])

    def test_bo3_one_nil_only_counts_match_point_side(self):
        # 实测：BO3 1:0 时第二张图落后方先到 10 分就开录了，结果打成 1:1，提前录了一整张图
        trailing = watcher.assess(make_match(finished=(1,), live_score=(4, 11)), CFG)
        self.assertTrue(trailing["deciding"])
        self.assertFalse(trailing["should_record"])
        self.assertEqual(trailing["interval"], CFG["intervals"]["idle"])
        self.assertTrue(watcher.assess(make_match(finished=(1,), live_score=(11, 4)), CFG)["should_record"])
        self.assertTrue(watcher.assess(make_match(finished=(1,), live_score=(4, 12)), CFG)["should_record"] is False)
        self.assertTrue(watcher.assess(make_match(finished=(1,), live_score=(12, 12)), CFG)["should_record"])  # 加时
        # 1:1 第三张图：两边都是赛点
        self.assertTrue(watcher.assess(make_match(finished=(1, 2), live_score=(3, 10)), CFG)["should_record"])

    def test_recording_pauses_when_series_continues(self):
        assessment = watcher.assess(make_match(finished=(1, 2), live_score=(1, 0)), CFG, recording=True)
        self.assertTrue(watcher.series_continued([1, 0], assessment))   # 开录时 1:0，现在 1:1
        self.assertFalse(watcher.series_continued([1, 1], assessment))  # 还在开录时那张图
        self.assertFalse(watcher.series_continued(None, assessment))    # 人工开录
        over = watcher.assess(make_match(status="over", finished=(1, 1)), CFG, recording=True)
        self.assertFalse(watcher.series_continued([1, 0], over))        # 系列赛结束了，正常走赛后流程

    def test_match_over_and_recording(self):
        self.assertTrue(watcher.assess(make_match(status="over", finished=(1, 1)), CFG)["should_record"])
        # 对手投降：HLTV 还显示 LIVE，但已经 2:0
        surrendered = watcher.assess(make_match(status="live", finished=(1, 1)), CFG)
        self.assertEqual((surrendered["status"], surrendered["should_record"]), ("over", True))
        recording = watcher.assess(make_match(finished=(1,), live_score=(11, 3)), CFG, recording=True)
        self.assertEqual(recording["interval"], CFG["intervals"]["recording"])

    def test_reload_rules(self):
        now = time.time() + 120
        self.assertFalse(match_sources.needs_reload(make_match(status="upcoming"), now - 120))  # 刚加载过
        self.assertFalse(match_sources.needs_reload(make_match(finished=(1,), live_score=(5, 3)), now))
        self.assertTrue(match_sources.needs_reload(make_match(finished=(1,), live_score=(13, 9)), now))
        self.assertTrue(match_sources.needs_reload(make_match(finished=(1,), live_score=(5, 3)), now + 3600))
        self.assertTrue(match_sources.needs_reload(make_match(status="upcoming"), now))
        switched = make_match(finished=(1,), live_score=(1, 0), board_map="Anubis")
        switched["maps"][-1]["name"] = "Mirage"
        self.assertTrue(match_sources.needs_reload(switched, now))


class Bo3Test(unittest.TestCase):
    def test_finished_match(self):
        match = bo3.normalize_match(bo3_fixture("match_over"))
        self.assertEqual((match["status"], match["best_of"], match["team1"]["name"]), ("over", 3, "fnatic"))
        self.assertEqual([(m["name"], m["status"], m["winner"], m["team1_score"], m["team2_score"]) for m in match["maps"]],
                         [("Ancient", "finished", 1, 13, 9), ("Mirage", "finished", 1, 13, 7)])
        self.assertTrue(watcher.assess(match, CFG)["should_record"])
        official = live.pick_stream(match["streams"])
        self.assertEqual((official["url"], official["country"]), ("https://kick.com/starladder", "Other"))

    def test_swapped_order_and_three_maps(self):
        match = bo3.normalize_match(bo3_fixture("match_over_3maps"), swapped=True)
        self.assertEqual((match["team1"]["name"], match["team2"]["name"]), ("Astralis", "Eternal Fire"))
        self.assertEqual([m["winner"] for m in match["maps"]], [1, 2, 2])
        self.assertEqual((match["maps"][0]["team1_score"], match["maps"][0]["team2_score"]), (13, 10))
        self.assertEqual((match["team1"]["score"], match["team2"]["score"]), (1, 2))

    def test_live_match_without_round_score(self):
        match = bo3.normalize_match(bo3_fixture("match_live"))
        self.assertEqual(match["status"], "live")
        self.assertEqual([m["status"] for m in match["maps"]], ["live", "upcoming", "upcoming"])
        self.assertFalse(watcher.assess(match, CFG)["should_record"])  # 第一张图不是决胜图
        # BO1 / 决胜图正在打、但没有回合比分：直接录
        decider = {**match, "best_of": 1}
        result = watcher.assess(decider, CFG)
        self.assertTrue(result["deciding"] and result["should_record"])
        self.assertEqual(result["interval"], CFG["intervals"]["recording"])

    def test_no_round_score_waits_for_deciding_map_to_run(self):
        # 实测：BO3 0:1、第二张图刚开打就开录太早；改为决胜图开打 N 分钟后再录
        raw = bo3_fixture("match_live")
        raw["team2_score"] = 1
        raw["games"][-1]["status"] = "finished"
        raw["games"][1]["status"] = "current"
        match = bo3.normalize_match(raw)
        current = next(m for m in match["maps"] if m["status"] == "live")
        wait = CFG["no_score_record_minutes"] * 60
        now = 1_800_000_000
        current["began_at"] = now - 5 * 60
        early = watcher.assess(match, CFG, now=now)
        self.assertTrue(early["deciding"])
        self.assertFalse(early["should_record"])
        self.assertEqual(early["interval"], CFG["intervals"]["mid"])
        current["began_at"] = now - wait + 60
        self.assertEqual(watcher.assess(match, CFG, now=now)["interval"], 65)  # 到点前一分钟：到点再查
        current["began_at"] = now - wait
        self.assertTrue(watcher.assess(match, CFG, now=now)["should_record"])
        current["began_at"] = None  # 不知道开打时间：保险起见直接录
        self.assertTrue(watcher.assess(match, CFG, now=now)["should_record"])

    def test_round_score_from_hltv_uses_normal_thresholds(self):
        raw = bo3_fixture("match_live")
        raw["team2_score"] = 1
        raw["games"][-1]["status"] = "finished"
        raw["games"][1]["status"] = "current"
        match = bo3.normalize_match(raw)
        name = next(m for m in match["maps"] if m["status"] == "live")["name"]
        with_score = {**match, "has_round_score": True,
                      "live": {"map": name, "round": 8, "team1": 3, "team2": 4, "source": "scoreboard"}}
        result = watcher.assess(with_score, CFG, now=1_800_000_000)
        self.assertFalse(result["should_record"])
        self.assertEqual(result["interval"], CFG["intervals"]["idle"])
        with_score["live"] = {**with_score["live"], "team2": CFG["record_at"]}
        self.assertTrue(watcher.assess(with_score, CFG)["should_record"])

    def test_map_key(self):
        self.assertEqual(watcher._map_key("de_dust2"), watcher._map_key("Dust 2"))
        self.assertNotEqual(watcher._map_key("Inferno"), watcher._map_key("Nuke"))

    def test_series_score_counts_before_map_winner_is_filled(self):
        # 实测：图刚打完时大比分已经是 1:0，这张图的胜方还没填
        raw = bo3_fixture("match_live")
        raw["team1_score"] = 1
        raw["games"][-1]["status"] = "finished"
        match = bo3.normalize_match(raw)
        self.assertEqual([m["winner"] for m in match["maps"]], [None, None, None])
        result = watcher.assess(match, CFG)
        self.assertEqual(result["map_wins"], [1, 0])
        self.assertTrue(result["deciding"])
        self.assertFalse(result["should_record"])  # 第二张图还没开打
        raw["games"][1]["status"] = "current"
        self.assertTrue(watcher.assess(bo3.normalize_match(raw), CFG)["should_record"])

    def test_find_match_by_names_and_time(self):
        results = bo3_fixture("list")["results"]
        start = bo3.parse_time("2026-10-30T12:00:00Z")
        found = bo3.find_match(results, "Nemiga", "Vitality", start)
        self.assertEqual((found[0]["slug"], found[1]), ("nemiga-vs-vitality-30-10-2026", False))
        self.assertTrue(bo3.find_match(results, "Team Vitality", "Nemiga Gaming", start + 3600)[1])
        self.assertIsNone(bo3.find_match(results, "Nemiga", "Vitality", start + 2 * 86400))
        self.assertIsNone(bo3.find_match(results, "Nemiga", "Falcons", start))

    def test_team_name_normalization(self):
        self.assertTrue(bo3.same_team("Falcons", ["Team Falcons"]))
        self.assertTrue(bo3.same_team("MOUZ", ["mouz cs2"]))
        self.assertTrue(bo3.same_team("Natus Vincere", ["Natus Vincere"]))
        self.assertFalse(bo3.same_team("BIG", ["BIG Academy"]) and bo3.same_team("G2", ["G2 Ares"]))
        self.assertEqual(bo3.map_display_name("de_dust2"), "Dust2")


class StreamTest(unittest.TestCase):
    def test_picks_official_stream(self):
        streams = [{"provider": "twitch", "name": "strogo", "country": "Russia", "viewers": 44089, "url": "u1"},
                   {"provider": "youtube", "name": "PGL", "country": "Other", "viewers": 0, "url": "u2"},
                   {"provider": "twitch", "name": "PGL", "country": "Other", "viewers": 34431, "url": "u3"}]
        self.assertEqual(live.pick_stream(streams)["url"], "u3")
        self.assertIsNone(live.pick_stream([]))
        official = live.pick_stream(P.parse_match(fixture("match_upcoming"))["streams"])
        self.assertEqual((official["name"], official["provider"]), ("PGL", "twitch"))

    def test_event_named_channel_beats_hltv_other(self):
        # 实测 DFRAG Invitational：HLTV 把个人主播标成 Other，官方 DFragTV 只在 bo3.gg 上且没标官方
        task = {"id": 999, "source_type": "live", "context": {"match": {"event": "DFRAG Invitational 2026", "streams": [
            {"provider": "kick", "name": "MominPK", "country": "Other", "viewers": 468, "url": "https://kick.com/mominpk"}]}}}
        state = {"streams": [{"provider": "kick", "name": "DFragTV", "country": "en", "viewers": 0,
                              "url": "https://kick.com/dfragtv", "source": "bo3"}]}
        with unittest.mock.patch.object(live, "remembered_streams", return_value=[]):
            picked = live.pick_stream(live.candidate_streams(task, state))
        self.assertEqual(picked["url"], "https://kick.com/dfragtv")
        self.assertEqual(live.event_keywords("PGL Masters Bucharest 2026"), ["pgl", "bucharest"])

    def test_candidate_streams_merge_and_rank(self):
        task = {"id": 1, "context": {"match": {"streams": [
            {"provider": "twitch", "name": "PGL", "country": "Other", "viewers": 5, "url": "https://www.twitch.tv/pgl"}]}}}
        state = {"streams": [
            {"provider": "kick", "name": "x", "country": "Other", "viewers": 900, "url": "https://kick.com/x", "source": "bo3"},
            {"provider": "twitch", "name": "PGL", "country": "Other", "viewers": 0, "url": "https://www.twitch.tv/PGL/", "source": "bo3"}]}
        merged = live.candidate_streams(task, state)
        self.assertEqual(len(merged), 2)  # 同一个地址只留一个
        self.assertEqual(live.pick_stream(merged)["url"], "https://www.twitch.tv/PGL/")

    def test_youtube_video_link_loses_to_channel_streams(self):
        # 实测 Nemiga vs Vitality：预约时 HLTV 给的 PGL YouTube 链接第二天已是前一天的回放
        streams = [
            {"provider": "youtube", "name": "PGL", "country": "Other", "viewers": 0,
             "url": "https://www.youtube.com/watch?v=swMquqtyh-A", "source": "hltv"},
            {"provider": "twitch", "name": "PGL", "country": "Other", "viewers": 205,
             "url": "https://www.twitch.tv/pgl", "source": "bo3"},
            {"provider": "kick", "name": "PGL", "country": "Other", "viewers": 252,
             "url": "https://kick.com/pgl", "source": "bo3"}]
        self.assertEqual(live.pick_stream(streams)["url"], "https://www.twitch.tv/pgl")
        self.assertEqual(live.pick_stream([streams[0], streams[2]])["url"], "https://kick.com/pgl")
        self.assertTrue(live.is_video_link("https://youtu.be/abc"))
        self.assertTrue(live.is_video_link("https://www.youtube.com/live/abc"))
        self.assertFalse(live.is_video_link("https://www.youtube.com/@PGL/live"))

    def test_bad_streams_are_excluded(self):
        task = {"id": 1, "context": {"match": {"streams": []}}}
        state = {"bad_streams": ["https://www.twitch.tv/pgl"], "streams": [
            {"provider": "twitch", "name": "PGL", "country": "Other", "url": "https://www.twitch.tv/PGL/", "source": "bo3"},
            {"provider": "kick", "name": "PGL", "country": "Other", "url": "https://kick.com/pgl", "source": "bo3"}]}
        with unittest.mock.patch.object(live, "remembered_streams", return_value=[]):
            merged = live.candidate_streams(task, state)
        self.assertEqual([s["url"] for s in merged], ["https://kick.com/pgl"])


class NotLiveTest(unittest.TestCase):
    def test_faster_than_realtime_means_replay(self):
        from backend.pipeline.recorder import is_faster_than_live, recorded_since
        self.assertFalse(is_faster_than_live(60, 30))    # 刚连上，不判断
        self.assertFalse(is_faster_than_live(75, 60))    # 直播：开头多下载了直播点之前的几个分片
        self.assertTrue(is_faster_than_live(160, 60))    # 回放：实测约 2.7 倍速
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            now = float(int(time.time()))
            write_chunk(directory, now - 100, 30, 1)      # 上一次连接的
            write_chunk(directory, now, 30, 1)
            write_chunk(directory, now + 10, 30, 1)
            self.assertAlmostEqual(recorded_since(directory, now), 60, places=3)


class DetectRetractTest(unittest.TestCase):
    def test_later_no_interview_answer_retracts_start(self):
        # 实测：一次判断把预录专访当成采访开始，之后都说没有采访，起点却一直留着，最后超时截了无关内容
        from backend import config
        from backend.pipeline import stages
        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp)
            lines = [{"start": t, "end": t + 20, "text": "casters talking"} for t in range(0, 2400, 20)]
            (workdir / "live_transcript.jsonl").write_text(
                "".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")
            (workdir / "live.json").write_text(json.dumps({
                "recording": {"started_at": 1000.0},
                "detection": {"started": True, "start": 2116.0, "started_at": time.time(), "checked_until": 2000},
            }), encoding="utf-8")
            fake_db = unittest.mock.MagicMock()
            fake_db.get_task.return_value = {"id": 1, "state": "recording", "title": "t", "context": {}}
            fake_db.get_settings.return_value = {**DEFAULT_SETTINGS, "live": {**CFG, "detect_interval": 0},
                                                 "llm": {"clean": {"provider_id": "x"}}}
            answer = '{"started": false, "ended": false, "note": "pre-recorded segment"}'
            session = live.LiveSession.__new__(live.LiveSession)
            session.task_id, session.workdir, session.manager = 1, workdir, unittest.mock.MagicMock()
            with unittest.mock.patch.object(config, "task_dir", return_value=workdir), \
                    unittest.mock.patch.object(live, "db", fake_db), \
                    unittest.mock.patch.object(stages, "_llm_call", return_value=answer):
                session._detect()
            detection = json.loads((workdir / "live.json").read_text(encoding="utf-8"))["detection"]
            self.assertFalse(detection["started"])
            self.assertNotIn("start", detection)
            self.assertIn("撤回", fake_db.add_event.call_args[0][1])

    def test_end_at_transcript_edge_is_not_accepted(self):
        # 实测：采访换人时 LLM 把转写最后一行当成结束，必须等结束点之后有足够内容才算
        from backend import config
        from backend.pipeline import stages
        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp)
            lines = [{"start": t, "end": t + 30, "text": "talk"} for t in range(0, 840, 30)]
            (workdir / "live_transcript.jsonl").write_text(
                "".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")
            (workdir / "live.json").write_text(json.dumps({"recording": {"started_at": 1000.0}}), encoding="utf-8")
            fake_db = unittest.mock.MagicMock()
            fake_db.get_task.return_value = {"id": 1, "state": "recording", "title": "t", "context": {}}
            fake_db.get_settings.return_value = {**DEFAULT_SETTINGS, "live": {**CFG, "detect_interval": 0},
                                                 "llm": {"clean": {"provider_id": "x"}}}
            session = live.LiveSession.__new__(live.LiveSession)
            session.task_id, session.workdir, session.manager = 1, workdir, unittest.mock.MagicMock()

            def detect(end: str):
                answer = json.dumps({"started": True, "start": "11:30", "ended": True, "end": end})
                with unittest.mock.patch.object(config, "task_dir", return_value=workdir), \
                        unittest.mock.patch.object(live, "db", fake_db), \
                        unittest.mock.patch.object(stages, "_llm_call", return_value=answer):
                    live.mutate_state(1, lambda s: s.setdefault("detection", {}).update(checked_until=-1))
                    session._detect()
                return json.loads((workdir / "live.json").read_text(encoding="utf-8"))["detection"]
            self.assertIsNone(detect("13:30")["pending_end"])   # 转写到 14:00，只多了 30 秒
            self.assertEqual(detect("12:30")["pending_end"], 780)  # 之后有 60 秒内容


def write_chunk(directory: Path, start: float, seconds: float, value: int) -> Path:
    path = directory / time.strftime("c_%Y%m%d_%H%M%S.wav", time.localtime(start))
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(SAMPLE_RATE)
        wav.writeframes(value.to_bytes(2, "little", signed=True) * int(seconds * SAMPLE_RATE))
    return path


class TimelineTest(unittest.TestCase):
    def test_timeline_chains_chunks_and_cut_fills_gaps(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            origin = float(int(time.time()))
            write_chunk(directory, origin, 60, 1)
            write_chunk(directory, origin + 59, 60, 2)   # 文件名时刻只精确到秒，实际紧接上一段
            write_chunk(directory, origin + 200, 30, 3)  # 断流重连
            chunks = sorted(directory.glob("c_*.wav"))
            timeline = live.chunk_timeline(chunks, chunk_start(chunks[0]))
            self.assertEqual([round(begin) for _, begin, _ in timeline], [0, 60, 200])

            out = directory / "cut.wav"
            self.assertAlmostEqual(live.cut_audio(timeline, 50, 210, out), 160, places=3)
            with wave.open(str(out), "rb") as wav:
                data = wav.readframes(wav.getnframes())

            def sample(t: float) -> int:
                index = int(t * SAMPLE_RATE) * 2
                return int.from_bytes(data[index:index + 2], "little", signed=True)
            # 50 秒起：第一段 → 第二段 → 断流期间的静音 → 重连后的第三段
            self.assertEqual([sample(5), sample(20), sample(100), sample(155)], [1, 2, 0, 3])

    def test_detection_window_and_line_end(self):
        lines = [{"start": t, "end": t + 20, "text": "x"} for t in range(0, 3000, 20)]
        self.assertGreaterEqual(live.detection_window(lines, None)[0]["end"], lines[-1]["end"] - live.DETECT_WINDOW)
        self.assertEqual(live.detection_window(lines, 600)[0]["start"], 520)
        self.assertEqual(live.line_end_at(lines, 160), 180)



def shots_from(spec):
    """spec: [(秒, 标签), ...] 每 5 秒一帧"""
    return [{"file": f"f{t}.jpg", "t": float(t), "label": label} for t, label in spec]


class ShotTimelineTest(unittest.TestCase):
    def test_interview_run_tolerates_short_cutaway(self):
        spec = [(t, "desk") for t in range(600, 700, 5)]
        spec += [(t, "interview") for t in range(700, 800, 5)]
        spec += [(800, "game"), (805, "game")]                     # 插了 10 秒回放
        spec += [(t, "interview") for t in range(810, 925, 5)]
        spec += [(t, "desk") for t in range(925, 1000, 5)]
        runs = live.interview_runs(shots_from(spec))
        self.assertEqual(runs, [{"start": 700.0, "end": 925.0, "open": False}])

    def test_run_still_open_until_cut_away_long_enough(self):
        spec = [(t, "interview") for t in range(700, 900, 5)] + [(900, "desk"), (905, "desk")]
        self.assertTrue(live.interview_runs(shots_from(spec))[0]["open"])
        self.assertIsNone(live.matching_run(live.interview_runs(shots_from(spec)), 400))
        self.assertEqual(live.matching_run(live.interview_runs(shots_from(spec)), 690)["start"], 700)

    def test_short_flash_is_ignored(self):
        spec = [(t, "desk") for t in range(0, 100, 5)] + [(100, "interview"), (105, "interview")]
        spec += [(t, "desk") for t in range(110, 200, 5)]
        self.assertEqual(live.interview_runs(shots_from(spec)), [])

    def test_sheet_and_labels(self):
        from PIL import Image
        with tempfile.TemporaryDirectory() as tmp:
            frames = []
            for i in range(9):
                path = Path(tmp) / f"f_{i}.jpg"
                Image.new("RGB", (320, 180), (i * 20, 0, 0)).save(path)
                frames.append((path, 600.0 + i * 5))
            sheet = Image.open(__import__("io").BytesIO(live.make_sheet(frames)))
            self.assertEqual(sheet.size, (960, 540))
        self.assertEqual(live.parse_shot_labels({"labels": ["Interview", "desk", "ad"]}, 3), ["interview", "desk", "other"])
        with self.assertRaises(ValueError):
            live.parse_shot_labels({"labels": ["desk"]}, 3)



class CameraDetectTest(unittest.TestCase):
    def run_detect(self, shots_spec, answers, transcript_until=1200):
        from backend import config
        from backend.pipeline import stages
        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp)
            lines = [{"start": t, "end": t + 30, "text": "talk"} for t in range(0, transcript_until, 30)]
            (workdir / "live_transcript.jsonl").write_text("".join(json.dumps(x) + "\n" for x in lines), encoding="utf-8")
            (workdir / "shots.jsonl").write_text("".join(json.dumps(x) + "\n" for x in shots_from(shots_spec)),
                                                 encoding="utf-8")
            (workdir / "live.json").write_text(json.dumps({"recording": {"started_at": 1000.0}}), encoding="utf-8")
            fake_db = unittest.mock.MagicMock()
            fake_db.get_task.return_value = {"id": 1, "state": "interview_detecting", "title": "t", "context": {}}
            fake_db.get_settings.return_value = {**DEFAULT_SETTINGS, "live": {**CFG, "detect_interval": 0},
                                                 "llm": {"clean": {"provider_id": "x"}}}
            session = live.LiveSession.__new__(live.LiveSession)
            session.task_id, session.workdir, session.manager = 1, workdir, unittest.mock.MagicMock()
            for answer in answers:
                with unittest.mock.patch.object(config, "task_dir", return_value=workdir), \
                        unittest.mock.patch.object(live, "db", fake_db), \
                        unittest.mock.patch.object(stages, "_llm_call", return_value=json.dumps(answer)):
                    live.mutate_state(1, lambda s: s.setdefault("detection", {}).update(checked_until=-1))
                    session._detect()
            return session.manager.capture

    def test_captures_when_camera_cuts_away(self):
        spec = [(t, "desk") for t in range(600, 700, 5)] + [(t, "interview") for t in range(700, 925, 5)]
        spec += [(t, "desk") for t in range(925, 1000, 5)]
        capture = self.run_detect(spec, [{"started": True, "start": "11:30", "ended": False}])
        capture.assert_called_once_with(1, 690, 925.0, "auto")

    def test_transcript_end_ignored_while_interview_shot_continues(self):
        # 实测换人提问时转写两次判定在 13:30 结束；画面上采访还在继续
        spec = [(t, "interview") for t in range(700, 1200, 5)]
        answer = {"started": True, "start": "11:30", "ended": True, "end": "13:30"}
        capture = self.run_detect(spec, [answer, answer])
        capture.assert_not_called()


if __name__ == "__main__":
    unittest.main()
