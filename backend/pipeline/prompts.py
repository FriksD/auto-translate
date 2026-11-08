"""
两段式写稿的提示词构建。

第一步（clean）：英文清洗——修正 ASR 错误、统一选手名、整理成 Q/A 问答稿。
第二步（write）：中文成稿——翻译润色为中文电竞媒体风格的 Q：/A： 问答稿。
"""
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

BEIJING = timezone(timedelta(hours=8))

CLEAN_SYSTEM = (
    "You are a senior CS2 esports editor. You clean up raw ASR (Whisper) transcripts of "
    "post-match interviews into accurate, readable English Q&A transcripts."
)

WRITE_SYSTEM = (
    "你是一位深耕CS2电竞领域多年的资深新闻主编，对职业生态、选手动态、战术体系有深刻理解。"
    "你的翻译不仅是语言转换，更是专业内容的再创作。"
)


def format_timestamp(seconds: float) -> str:
    seconds = max(0, int(seconds))
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


def format_transcript(transcript: Dict[str, Any]) -> str:
    lines = []
    for seg in transcript.get("segments", []):
        speaker = seg.get("speaker")
        prefix = f"[{format_timestamp(seg['start'])}]"
        if speaker:
            prefix += f" {speaker}:"
        lines.append(f"{prefix} {seg['text']}")
    return "\n".join(lines)


def format_name_table(context: Dict[str, Any], canonical_names: List[str]) -> str:
    lines = []
    listed = set()
    for team in context.get("teams") or []:
        team_name = team.get("name")
        if not team_name:
            continue
        details = {d.get("name"): d for d in team.get("player_details") or [] if isinstance(d, dict)}
        lines.append(f"战队 {team_name}")
        listed.add(team_name)
        for nick in team.get("players") or []:
            detail = details.get(nick, {})
            extra = "，".join(x for x in [detail.get("real_name"), detail.get("nationality")] if x)
            lines.append(f"  - {nick}" + (f"（{extra}）" if extra else ""))
            listed.add(nick)
        coach = (team.get("stats") or {}).get("coach") or team.get("coach")
        if coach:
            lines.append(f"  - 教练 {coach}")
            listed.add(coach)
    others = [n for n in canonical_names if n not in listed]
    if others:
        lines.append("其他规范名：" + "、".join(others))
    return "\n".join(lines) if lines else "（无）"


def _format_date(unix_ms: Any) -> str:
    if not unix_ms:
        return ""
    return datetime.fromtimestamp(unix_ms / 1000, tz=BEIJING).strftime("%Y-%m-%d %H:%M（北京时间）")


def _format_match(match: Dict[str, Any], teams: List[Dict[str, Any]]) -> List[str]:
    names = [t.get("name", "") for t in teams] + ["", ""]
    lines = [f"赛事：{match.get('event', '')}；{match.get('format', '')}；{_format_date(match.get('unix_ms'))}"]
    score = match.get("score") or []
    if match.get("status") == "over" and len(score) == 2 and None not in score:
        lines.append(f"最终比分：{names[0]} {score[0]}-{score[1]} {names[1]}")
    elif match.get("status") == "live":
        lines.append("比赛状态：进行中")
    maps = [
        f"{m['name']} {m['team1_score']}-{m['team2_score']}" + (f"（{m['picked_by']} 选）" if m.get("picked_by") else "")
        for m in match.get("maps") or [] if m.get("team1_score") is not None
    ]
    if maps:
        lines.append("各图比分（前者为" + names[0] + "）：" + "；".join(maps))
    if match.get("veto"):
        lines.append("BP：" + "；".join(match["veto"]))
    h2h = match.get("head_to_head")
    if h2h:
        lines.append(f"历史交手：{names[0]} {h2h['team1_wins']} 胜，{names[1]} {h2h['team2_wins']} 胜")
    return lines


def _format_team(team: Dict[str, Any]) -> List[str]:
    ranks = []
    if team.get("hltv_rank"):
        ranks.append(f"HLTV 排名 #{team['hltv_rank']}")
    if team.get("vrs_rank"):
        ranks.append(f"VRS 排名 #{team['vrs_rank']}")
    lines = [f"- {team['name']}" + (f"（{'，'.join(ranks)}）" if ranks else "")]
    standins = [f"{p['name']} 替补 {p['standin_for']}" for p in team.get("player_details") or [] if p.get("standin_for")]
    if standins:
        lines.append("  替补：" + "；".join(standins))
    past = team.get("past_matches") or []
    if past:
        label = {"won": "胜", "lost": "负", "draw": "平"}
        lines.append("  近期战绩：" + "；".join(
            f"{label.get(m['result'], '')} {m['opponent']} {m['score']}" for m in past[:6]
        ))
    maps = team.get("map_stats") or []
    if maps:
        lines.append("  近 3 个月图池：" + "；".join(
            f"{m['map']} 胜率 {m['win_pct']}（{m.get('wins')}胜{m.get('losses')}负，选图率 {m.get('pick_pct')}%）"
            for m in maps[:7]
        ))
    news = team.get("news") or []
    if news:
        lines.append("  近期新闻：" + "；".join(n["title"] for n in news[:5]))
    return lines


def format_background(context: Dict[str, Any]) -> str:
    teams = [t for t in context.get("teams") or [] if t.get("name")]
    parts: List[str] = []
    if context.get("match"):
        parts.extend(_format_match(context["match"], teams))
    for team in teams:
        parts.extend(_format_team(team))
    if context.get("background"):
        parts.append(context["background"])
    return "\n".join(parts) if parts else "（无）"


def several_interviewees(context: Dict[str, Any]) -> List[str]:
    """识别出的受访者（两人以上才算多人采访，否则返回空）。"""
    names = ((context or {}).get("identified") or {}).get("interviewees") or []
    return names if len(names) >= 2 else []


# 英文稿里以选手名开头的回答（多人采访），和 docx_export.SPEAKER 一致
NAMED_ANSWER = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9_.|!\-]{0,23})\s*:", re.M)


def answer_speakers(clean_text: str) -> List[str]:
    """英文稿里的回答人（Q、A 以外的前缀），按出现顺序去重。"""
    names: List[str] = []
    for name in NAMED_ANSWER.findall(clean_text):
        if name.upper() not in ("Q", "A") and name not in names:
            names.append(name)
    return names


def clean_format(context: Dict[str, Any]) -> str:
    names = several_interviewees(context)
    if names:
        return f"""This is a group interview: several players answer ({', '.join(names)} detected from the broadcast; the list may be incomplete or misspelled).
Output format — plain text, no markdown:
Q: <interviewer question, one paragraph>
<player name>: <that player's answer, one paragraph; keep the whole answer in one paragraph>
(blank line between each question and answer)
Start every answer with the canonical name of the player or coach who gives it, e.g. "xertioN: Super happy...". Never write "A:". The question usually says who it is for ("what about you, Spinx?", "XertioN, can you come here?"); when two players answer one question, write two answers, one per player."""
    return """Output format — plain text, no markdown:
Q: <interviewer question, one paragraph>
A: <answer, one paragraph; keep the whole answer in one paragraph>
(blank line between each Q and A)
If it turns out several players or coaches answer in this interview, start each answer with the speaker's canonical name instead of "A", e.g. "xertioN: <answer>"."""


def build_clean_prompt(transcript: Dict[str, Any], context: Dict[str, Any],
                       canonical_names: List[str], aliases: Dict[str, str]) -> str:
    alias_lines = "\n".join(f"- {a} → {t}" for a, t in aliases.items()) or "(none)"
    return f"""Below is a raw Whisper transcript of a CS2 post-match interview, possibly with surrounding broadcast audio (casters, ads, crowd).

Your job:
1. Keep only the interview itself (interviewer questions and player/coach answers). Drop caster talk, ads and unrelated chatter before or after the interview.
2. Fix ASR errors. Whisper is very unreliable on player nicknames: restore every misheard name to the exact canonical spelling in the name table (case, digits and symbols must match exactly, e.g. m0NESY, ZywOo). A player's real name should also be replaced with their nickname. Use pronunciation similarity and context (team, role) to decide.
3. Remove filler words and false starts, but do not drop any meaningful content, opinion or tone.
4. Mark words you cannot recover with [?].
5. If the interview is interpreted (e.g. a translator relays a Russian answer), keep only the English answer.

{clean_format(context)}
If there are no interview contents at all, output exactly: NO_INTERVIEW

Name table (canonical spellings, from HLTV):
{format_name_table(context, canonical_names)}

Known aliases:
{alias_lines}

Raw transcript:
{format_transcript(transcript)}
"""


def write_format(clean_text: str) -> str:
    speakers = answer_speakers(clean_text)
    if speakers:
        return f"""这是多人采访（{'、'.join(speakers)}），英文稿里每段回答以回答人的名字开头。输出格式严格如下，不要使用任何 markdown 符号，不要标题、导语或注释：
Q：<主持人问题，一段>
<选手名>：<这位选手的回答，一段>
回答人的名字照抄英文稿（例如「xertioN：超级开心……」），不要写成「A：」。每个问题与回答之间空一行。回答很长时可以按意群拆成多段，后续段落不加前缀。"""
    return """输出格式严格如下，不要使用任何 markdown 符号，不要标题、导语或注释：
Q：<主持人问题，一段>
A：<选手回答，一段>
每个 Q 与 A 之间空一行。回答很长时可以按意群拆成多段，后续段落不加前缀。"""


def build_write_prompt(clean_text: str, context: Dict[str, Any], canonical_names: List[str],
                       terms: Dict[str, str], notes: str, extra_instructions: str) -> str:
    term_lines = "\n".join(f"- {t} → {zh}" for t, zh in terms.items()) or "（无）"
    sections = [f"""将下面的CS2赛后采访英文问答稿翻译润色为中文，产出可直接发表于中文电竞媒体的采访稿。

要求：
1. 忠于原意，不遗漏任何核心观点、细节和说话人的语气，但禁止逐字直译，要符合中文行文习惯。
2. CS2 专业术语使用中国大陆电竞社区的惯用说法，优先遵循下方术语表。
3. 选手名、教练名、战队名一律保留英文，并且必须与【规范名表】的写法完全一致（大小写、数字、符号都不能改），例如 m0NESY、ZywOo，不要音译成中文。
4. {write_format(clean_text)}
5. 原稿中 [?] 标记的内容如果无法从上下文推断，在译文对应位置保留 [?]，方便人工核对。

【规范名表（来自 HLTV）】
{format_name_table(context, canonical_names)}

【术语表】
{term_lines}

【比赛背景（仅供理解语境，不要写进稿件）】
{format_background(context)}"""]
    if notes:
        sections.append(f"【本稿要求】\n{notes}")
    if extra_instructions:
        sections.append(f"【补充要求】\n{extra_instructions}")
    sections.append(f"【英文问答稿】\n{clean_text}")
    return "\n\n".join(sections) + "\n"


def whisper_prompt(canonical_names: List[str]) -> str:
    """Whisper initial_prompt：用规范名提高专有名词识别率（长度有限，最多取 60 个）。"""
    if not canonical_names:
        return ""
    return "CS2 post-match interview. Players and teams: " + ", ".join(canonical_names[:60]) + "."


NAME_RESOLVE_SYSTEM = "You are a CS2 esports fact checker. You answer with strict JSON only."


def build_name_resolution_prompt(contexts: Dict[str, List[str]], context: Dict[str, Any],
                                 canonical_names: List[str]) -> str:
    """让 LLM 结合上下文判断稿件中残留的英文词是否是被识别错的选手/战队名。"""
    items = "\n".join(
        f'- "{token}": ' + " | ".join(f"…{s}…" for s in snippets) for token, snippets in contexts.items()
    )
    return f"""下面是一篇 CS2 赛后采访中文稿里残留的英文词，以及它们在稿件中的上下文。
原始音频经过语音识别，选手名、战队名常被误识别成发音相近的普通单词或错误拼写（例如 sh1ro 被识别成 Shearer）。

请结合上下文和发音，判断每个词是否其实指的是【规范名表】中的某个名字：
- 是：给出规范名表中的写法（必须逐字一致）
- 不是（普通词汇、地图、赛事、术语，或无法确定）：给出 null

只输出一个 JSON 对象，键为原词，值为规范名或 null，不要输出任何其他内容。

【规范名表】
{format_name_table(context, canonical_names)}

【待判断的词】
{items}
"""


# ── 直播采访定位 ──────────────────────────────────────────────────────────────

def parse_clock(text: Any) -> Optional[float]:
    """"12:34" / "1:02:03" / 754 → 秒数"""
    if text is None or text == "":
        return None
    if isinstance(text, (int, float)):
        return float(text)
    parts = str(text).strip().split(":")
    try:
        values = [float(p) for p in parts]
    except ValueError:
        return None
    seconds = 0.0
    for value in values:
        seconds = seconds * 60 + value
    return seconds


def format_live_lines(lines: List[Dict[str, Any]]) -> str:
    return "\n".join(f"[{format_timestamp(line['start'])}] {line['text']}" for line in lines)


INTERVIEW_SCAN_SYSTEM = (
    "You monitor the live broadcast of a CS2 esports event. You read a rolling, timestamped speech-to-text "
    "transcript of the stream and find live player interviews. You answer with strict JSON only."
)


def build_interview_scan_prompt(lines: List[Dict[str, Any]], event: str, teams: str = "", shots: str = "") -> str:
    """直播间监视用的采访定位：不绑定比赛，找出这段转写里的所有现场采访。"""
    return f"""Event: {event or 'unknown'}
{('Teams at this event (with players, may be incomplete):' + chr(10) + teams + chr(10)) if teams else ''}
Timestamps are [mm:ss] (minutes may exceed 59) since the recording started. The transcript is machine-generated, so names may be misspelled.

Transcript:
{format_live_lines(lines)}
{shots}
A live interview is a host or interviewer asking a player or coach questions and the player answering, live on the broadcast (on stage, in front of a sponsor wall, on the desk as a guest). It can happen before a match, between maps, after a match, or be about anything. Casters' analysis, analysts' desk talk among themselves, replays, ads, highlight videos and pre-recorded or edited features are NOT live interviews.

List every live interview in the transcript, in time order. Answer with JSON:
{{"interviews": [{{"start": "mm:ss", "ended": bool, "end": "mm:ss" or null, "interviewees": [player names], "team": team name or null, "live": bool, "note": short reason}}]}}

Rules:
- "start" is the line where the interviewer starts (greeting or first question).
- One interview per continuous conversation, not per player. When the same interview goes on to the next player without other content in between (e.g. "and now I'm joined by X", "what about you, Y?", a winning team answering one by one on stage), it is still the same interview: list all of them as its interviewees, in the order they answer. Start a new interview only when other content (casters, analysts, ads, a ceremony, replays) comes in between, or a different interviewer starts a separate interview.
- A change of camera angle, the crowd cheering or a short pause does not end an interview.
- Set "ended" true only if the transcript clearly continues with other content after the interview (casters, analysts, ads, the next segment, or the next interview). If the transcript ends while the interview may still be going, set "ended" false. "end" is the last line of the interview.
- Set "live" false for pre-recorded or edited features and for clips played from earlier; they are still listed so they are not mistaken for live interviews.
- If there is no interview, return {{"interviews": []}}."""


SHOT_LABELS = ("interview", "desk", "game", "other")

SHOT_SYSTEM = (
    "You label frames from the live broadcast of a CS2 esports match. You answer with strict JSON only."
)


def build_shot_prompt(count: int) -> str:
    return f"""The image is a grid of {count} frames from the broadcast in time order (left to right, top to bottom). Each frame is labelled with its timestamp.

Give each frame one label:
- interview: a live interview — a host holding a microphone talking with one or more players or a coach, usually standing in front of a sponsor wall or on stage.
- desk: casters or analysts at a desk, sofa or studio (also when a player sits with them).
- game: in-game footage, replays, the game HUD, scoreboards, stats or map veto screens.
- other: ads, trailers, pre-recorded or edited features (including sit-down player features), highlight videos, break screens, crowd shots, anything else.

Answer with JSON: {{"labels": [...]}} with exactly {count} labels in frame order."""


def format_shot_runs(runs: List[Dict[str, Any]]) -> str:
    """把画面识别出的采访镜头段落写进采访定位的提示词。"""
    if not runs:
        return ""
    parts = []
    for run in runs:
        end = "still on screen" if run["open"] else f"until [{format_timestamp(run['end'])}]"
        parts.append(f"[{format_timestamp(run['start'])}] {end}")
    return ("\nCamera: an interview shot (host with microphone and player) is on screen from "
            + "; ".join(parts) + ". Use it to place the start and end, but the transcript decides who is "
            "talking and whether it is a live post-match interview.\n")


# ── 投稿信息（标题、摘要） ────────────────────────────────────────────────────

PACKAGE_SYSTEM = (
    "你是一位深耕CS2电竞领域多年的资深新闻编辑，负责给中文电竞媒体的赛后采访稿拟标题、写导语。"
    "你只输出严格的 JSON。"
)


def _format_facts(facts: Dict[str, Any]) -> str:
    lines = [f"赛事（英文）：{facts.get('event') or '未知'}"]
    if facts.get("stage"):
        lines.append(f"赛段：{facts['stage']}" + (f"（BO{facts['best_of']}）" if facts.get("best_of") else ""))
    teams = facts.get("teams") or []
    if len(teams) == 2:
        lines.append(f"对阵：{teams[0]} vs {teams[1]}")
    if facts.get("winner") and facts.get("score"):
        lines.append(f"结果：{facts['winner']} {facts['score']} 击败 {facts['loser']}")
    else:
        lines.append("结果：比分未知（不要编造比分，摘要里可以不写比分）")
    if facts.get("maps"):
        lines.append("各图：" + "；".join(facts["maps"]))
    if facts.get("date"):
        lines.append(f"比赛日期：{facts['date']}（北京时间）")
    if facts.get("just_ended"):
        lines.append("比赛刚刚结束（直播中录到的采访），摘要可以用「就在刚刚」")
    if facts.get("interviewee_guess"):
        team = f"（{facts['interviewee_team']}）" if facts.get("interviewee_team") else ""
        lines.append(f"直播判断的受访者：{facts['interviewee_guess']}{team}（可能不准，以稿件为准）")
    return "\n".join(lines)


# 采访类型 → 标题里的叫法、摘要的写法（None：按赛后采访的范例写）
INTERVIEW_KINDS = {
    "just_played": ("赛后采访", None),
    "between_maps": ("采访", "这是系列赛图间的采访，比赛还没打完：摘要不写比分和胜负，写成「赛事中文名今日继续；"
                           "在X对阵Y的比赛中，受访选手在图间接受了主持人的采访，谈论了…」。"),
    "upcoming": ("赛前采访", "这是赛前采访，比赛还没打：摘要不写比分和胜负，写成「赛事中文名今日继续；"
                          "在X对阵Y的比赛开始前，受访选手接受了主持人的采访，谈论了…」（对手不明确就不写对阵）。"),
    "general": ("采访", "这次采访不是针对某一场比赛：摘要不写比分和胜负，写成「赛事中文名今日继续；"
                      "受访选手（所在战队）接受了主持人的采访，谈论了…」。"),
    "unknown": ("采访", "不确定这次采访对应哪场比赛：摘要不写比分和胜负，写成「赛事中文名今日继续；"
                      "受访选手（所在战队）接受了主持人的采访，谈论了…」。"),
}


def interview_kind(facts: Dict[str, Any]) -> Tuple[str, Optional[str]]:
    """（标题里的叫法, 摘要的额外要求）"""
    kind = facts.get("interview_kind") or "just_played"
    label, rule = INTERVIEW_KINDS.get(kind, INTERVIEW_KINDS["unknown"])
    if kind == "just_played" and not facts.get("score"):
        rule = "比分未知（没能确认是哪场比赛）：摘要不写比分和胜负，只写「比赛结束后，受访选手接受了主持人的采访，谈论了…」。"
    if rule and facts.get("opponent") and facts.get("interviewee_team"):
        rule = rule.replace("X对阵Y", f"{facts['interviewee_team']}对阵{facts['opponent']}")
    return label, rule


def build_package_prompt(facts: Dict[str, Any], draft: str, title_examples: List[str], summary_examples: List[str],
                         event_terms: List[Tuple[str, str]], host: Optional[str], host_names: List[str]) -> str:
    titles = "\n".join(f"- {t}" for t in title_examples) or "（无）"
    summaries = "\n".join(f"- {t}" for t in summary_examples) or "（无）"
    terms = "\n".join(f"- {en} → {zh}" for en, zh in event_terms) or "（术语表里没有，请自己翻译）"
    if host:
        host_line = f"这项赛事的采访主持人一般是 {host}；稿件里能看出是别人时以稿件为准。"
    elif host_names:
        host_line = f"常见主持人：{'、'.join(host_names)}。稿件里能看出主持人是谁就写，看不出就写「接受了采访」，不要猜。"
    else:
        host_line = "稿件里能看出主持人是谁就写，看不出就写「接受了采访」，不要猜。"
    label, kind_rule = interview_kind(facts)
    summary_rule = f"\n5. {kind_rule}" if kind_rule else ""
    return f"""为下面这篇 CS2 选手{label}的中文稿拟投稿用的标题和摘要。

【比赛事实】（比分、队名以此为准，不要改动）
{_format_facts(facts)}

【赛事中文名】
术语表：
{terms}
写成「中文名-年份」的格式，例如「PGL布加勒斯特大师赛-2026」。

【标题】
近期发过的标题（学习格式和语气）：
{titles}
要求：
1. 给出 4 个候选，按推荐程度排序，第一个最好。
2. 格式「受访选手{label}：核心观点」，选手名必须和稿件里的写法完全一致。
3. 核心观点从采访里提炼最有信息量、最有话题性的一句，用选手的第一人称口吻，像引语一样自然，一般 15～30 个字；4 个候选尽量选不同的观点。
4. 不要加引号、书名号、序号，不要编造稿件里没有的内容。

【摘要】
范例：
{summaries}
要求：
1. 一句话（这句话会作为导语放在 Word 正文最前面），照范例的格式：赛事中文名 + 今日继续 / 今日正式落下帷幕 +（比赛刚结束时加「就在刚刚，」）+ 胜者以X-Y的比分击败败者 + 这场比赛的结果（晋级、淘汰、名次，按赛段判断；拿不准就不写）+ ；比赛结束后，受访选手接受了主持人的采访，谈论了…（用一句话概括采访的主要内容，如队伍目前的困境与后续的打算、对手的表现、赛程安排，只写稿件里确实谈到的，不要编造）。不要自己加「听译」备注，程序会加。
2. 决赛、季军赛写「今日正式落下帷幕」，其他比赛写「今日继续」。
3. {host_line}
4. 队名、选手名保留英文，和比赛事实、稿件的写法一致。{summary_rule}

【多人采访】稿件里的回答以不同的选手名开头（如「xertioN：」「Spinx：」）时，是同一场采访里问了好几个人：
标题写「战队名{label}：核心观点」（核心观点仍是某一位选手的原话）；摘要写「……；比赛结束后，X、Y、Z接受了主持人的采访，……」，按出场顺序列出受访者；JSON 里 interviewee 填说得最多的那位。

【输出】只输出一个 JSON 对象：
{{"interviewee": "受访选手（稿件里的写法）", "interviewee_team": "受访选手所在战队", "host": "主持人或 null", "event_zh": "赛事中文名", "titles": ["…", "…", "…", "…"], "summary": "…"}}

【中文稿】
{draft}
"""
