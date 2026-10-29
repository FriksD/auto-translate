"""
两段式写稿的提示词构建。

第一步（clean）：英文清洗——修正 ASR 错误、统一选手名、整理成 Q/A 问答稿。
第二步（write）：中文成稿——翻译润色为中文电竞媒体风格的 Q：/A： 问答稿。
"""
from typing import Any, Dict, List

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


def format_background(context: Dict[str, Any]) -> str:
    parts = []
    for team in context.get("teams") or []:
        if not team.get("name"):
            continue
        line = f"- {team['name']}"
        ranking = team.get("ranking")
        if ranking and ranking != "N/A":
            line += f"，世界排名 #{ranking}"
        parts.append(line)
        recent = team.get("recent_matches") or []
        if recent:
            results = "；".join(
                f"{m.get('team1')} {m.get('score')} {m.get('team2')}（{m.get('event')}）" for m in recent[:5]
            )
            parts.append(f"  近期比赛：{results}")
    if context.get("match_summary"):
        parts.append(f"本场比赛：{context['match_summary']}")
    if context.get("background"):
        parts.append(context["background"])
    return "\n".join(parts) if parts else "（无）"


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

Output format — plain text, no markdown:
Q: <interviewer question, one paragraph>
A: <answer, one paragraph; keep the whole answer in one paragraph>
(blank line between each Q and A)
If there are no interview contents at all, output exactly: NO_INTERVIEW

Name table (canonical spellings, from HLTV):
{format_name_table(context, canonical_names)}

Known aliases:
{alias_lines}

Raw transcript:
{format_transcript(transcript)}
"""


def build_write_prompt(clean_text: str, context: Dict[str, Any], canonical_names: List[str],
                       terms: Dict[str, str], notes: str, extra_instructions: str) -> str:
    term_lines = "\n".join(f"- {t} → {zh}" for t, zh in terms.items()) or "（无）"
    sections = [f"""将下面的CS2赛后采访英文问答稿翻译润色为中文，产出可直接发表于中文电竞媒体的采访稿。

要求：
1. 忠于原意，不遗漏任何核心观点、细节和说话人的语气，但禁止逐字直译，要符合中文行文习惯。
2. CS2 专业术语使用中国大陆电竞社区的惯用说法，优先遵循下方术语表。
3. 选手名、教练名、战队名一律保留英文，并且必须与【规范名表】的写法完全一致（大小写、数字、符号都不能改），例如 m0NESY、ZywOo，不要音译成中文。
4. 输出格式严格如下，不要使用任何 markdown 符号，不要标题、导语或注释：
Q：<主持人问题，一段>
A：<选手回答，一段>
每个 Q 与 A 之间空一行。回答很长时可以按意群拆成多段，后续段落不加前缀。
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
