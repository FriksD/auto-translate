"""
比赛数据源：比赛监视通过这里读取比分，按设置里的顺序依次尝试，前一个失败就用下一个。

每个数据源返回同一种结构（和 hltv_parse.parse_match 一致，另加几个字段）：
  status           upcoming / live / over
  best_of, maps[{name, team1_score, team2_score, status, winner}], team1/team2{name}, streams
  live             当前地图的实时比分 {map, round, team1, team2, source}，拿不到为 None
  has_round_score  数据源是否提供回合级比分；没有时决胜图一开打就录制
  source, url, loaded_at

- bo3：bo3.gg 公开接口，不需要浏览器、不会被 Cloudflare 拦截；普通比赛没有回合比分。
- hltv：浏览器标签页里的 HLTV 比赛页，有实时比分板；偶尔需要过 Cloudflare 验证。
"""
import time
from typing import Any, Dict, List, Optional

from .. import config, db
from ..sources.bo3 import Bo3Client
from ..sources.hltv import HLTVClient
from .live import load_state, patch_state

RELOAD_MAX_AGE = 600
RELOAD_MIN_AGE = 90
SOURCE_LABELS = {"bo3": "bo3.gg", "hltv": "HLTV"}
DEFAULT_ORDER = ["bo3", "hltv"]


class SourceUnavailable(RuntimeError):
    """这个数据源上找不到这场比赛（不是网络错误）。"""


class AllSourcesFailed(RuntimeError):
    pass


# ── HLTV 专用的纯函数 ────────────────────────────────────────────────────────

def live_map_score(match: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """当前地图的比分（按 team1/team2）。实时比分板优先，地图栏里的比分不会实时刷新。"""
    board = match.get("scoreboard")
    if board:
        names = {(match["team1"].get("name") or "").lower(): "team1",
                 (match["team2"].get("name") or "").lower(): "team2"}
        scores = {}
        for side in ("ct", "t"):
            key = names.get((board[side]["team"] or "").lower())
            if key:
                scores[key] = board[side]["score"]
        if len(scores) == 2:
            return {"map": board["map"], "round": board["round"], "team1": scores["team1"],
                    "team2": scores["team2"], "source": "scoreboard"}
    live = next((m for m in match["maps"] if m.get("status") == "live"), None)
    if live and live["team1_score"] is not None and live["team2_score"] is not None:
        return {"map": live["name"], "round": None, "team1": live["team1_score"],
                "team2": live["team2_score"], "source": "maps"}
    return None


def needs_reload(match: Dict[str, Any], now: float) -> bool:
    """没刷新页面时读到的内容是否可能过时（地图胜负、比赛状态只在刷新后更新）。"""
    age = now - (match.get("loaded_at") or 0)
    if age < RELOAD_MIN_AGE:
        return False  # 刚加载过，连续刷新容易触发 Cloudflare 验证
    if age > RELOAD_MAX_AGE:
        return True
    if match["status"] != "live":
        return True
    board = match.get("scoreboard")
    if board is None:
        return True
    live = next((m for m in match["maps"] if m.get("status") == "live"), None)
    if live is None or live["name"].lower() != board["map"].lower():
        return True
    high, low = max(board["ct"]["score"], board["t"]["score"]), min(board["ct"]["score"], board["t"]["score"])
    return high >= 13 and high > low  # 这张图可能刚打完


# ── 数据源 ───────────────────────────────────────────────────────────────────

class HLTVSource:
    name = "hltv"

    def __init__(self, client: HLTVClient):
        self.client = client

    async def read(self, task: Dict[str, Any]) -> Dict[str, Any]:
        if not task.get("match_hltv_id"):
            raise SourceUnavailable("任务没有 HLTV 比赛 ID")
        match_id = int(task["match_hltv_id"])
        match = await self.client.watch_match(match_id)
        if needs_reload(match, time.time()):
            match = await self.client.watch_match(match_id, reload=True)
        return {**match, "live": live_map_score(match), "has_round_score": True, "source": self.name}

    async def release(self, task: Dict[str, Any]) -> None:
        if task.get("match_hltv_id"):
            await self.client.unwatch_match(int(task["match_hltv_id"]))


class Bo3Source:
    name = "bo3"

    async def read(self, task: Dict[str, Any]) -> Dict[str, Any]:
        client = Bo3Client(config.proxy_or_none(db.get_settings()))
        link = (load_state(task["id"]).get("source_ids") or {}).get("bo3")
        if not link:
            if not task.get("team1") or not task.get("team2"):
                raise SourceUnavailable("队伍还没确定")
            found = await client.find(task["team1"], task["team2"], task.get("match_time"))
            if not found:
                raise SourceUnavailable("bo3.gg 上没有找到这场比赛")
            link = {"slug": found[0], "swapped": found[1]}
            set_source_id(task["id"], "bo3", link)
            db.add_event(task["id"], f"已关联 bo3.gg 比赛：{link['slug']}")
        return await client.get_match(link["slug"], bool(link.get("swapped")))

    async def release(self, task: Dict[str, Any]) -> None:
        return None


def set_source_id(task_id: int, source: str, value: Optional[Dict[str, Any]]) -> None:
    state = load_state(task_id)
    ids = dict(state.get("source_ids") or {})
    if value is None:
        ids.pop(source, None)
    else:
        ids[source] = value
    patch_state(task_id, source_ids=ids)


class SourceChain:
    def __init__(self, hltv: HLTVClient):
        self.sources = {"bo3": Bo3Source(), "hltv": HLTVSource(hltv)}

    def order(self, cfg: Dict[str, Any]) -> List[str]:
        names = [n for n in (cfg.get("sources") or DEFAULT_ORDER) if n in self.sources]
        return names or DEFAULT_ORDER

    async def read(self, task: Dict[str, Any], cfg: Dict[str, Any]) -> Dict[str, Any]:
        """按顺序读取，返回第一个成功的结果；附带前面失败的数据源和原因（failures）。"""
        failures = []
        for name in self.order(cfg):
            try:
                match = await self.sources[name].read(task)
            except Exception as error:
                failures.append({"source": name, "error": brief(error)})
                continue
            return {**match, "failures": failures}
        raise AllSourcesFailed("；".join(f"{SOURCE_LABELS[f['source']]}：{f['error']}" for f in failures))

    async def release(self, task: Dict[str, Any]) -> None:
        for source in self.sources.values():
            try:
                await source.release(task)
            except Exception:
                pass


def brief(error: Exception) -> str:
    """Playwright 的异常信息带整段浏览器日志，只保留第一行。"""
    return (str(error).strip().splitlines() or [type(error).__name__])[0][:300]
