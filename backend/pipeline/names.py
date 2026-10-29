"""
专有名词规范化：保证稿件中的选手名、战队名与 HLTV 写法完全一致。

思路：每个规范名生成一个"匹配键"（小写 + leet 数字归一 + 去符号），
稿件里的拉丁字母词（含相邻 1～3 个词的组合）只要匹配键命中，就替换成规范写法。
例如 m0NESY / monesy / Mo Nesy 的匹配键都是 "monesy"。

只对中文稿件做确定性替换：中文稿里的拉丁字母词几乎都是专有名词，
误伤普通英文单词（如 s1mple ↔ simple）的风险很低。
"""
import difflib
import re
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Set, Tuple

LEET_MAP = str.maketrans({"0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "7": "t", "$": "s", "@": "a"})
LATIN_TOKEN = re.compile(r"[A-Za-z0-9$@]+")
# 组合词之间允许的分隔字符（如 "Natus Vincere"、"Virtus.pro"、"Mo-Nesy"）
JOINER = re.compile(r"^[ .\-'_]{1,2}$")
MAX_SPAN = 3
SUSPECT_RATIO = 0.8

# 稿件中常见、无需提示的英文词（大小写不敏感）
COMMON_WORDS = {
    "cs", "cs2", "csgo", "hltv", "major", "mvp", "evp", "igl", "awp", "awper", "ak", "m4", "ct", "t",
    "bo1", "bo3", "bo5", "gg", "ot", "eco", "clutch", "ace", "entry", "rifler", "lurker", "anchor",
    "dust2", "mirage", "inferno", "nuke", "overpass", "ancient", "anubis", "vertigo", "train", "cache",
    "iem", "esl", "blast", "pgl", "pro", "league", "cologne", "katowice", "q", "a", "vs",
}


def name_key(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower().translate(LEET_MAP))


@dataclass
class NameReport:
    replaced: Dict[Tuple[str, str], int] = field(default_factory=dict)
    suspects: Dict[str, List[str]] = field(default_factory=dict)
    ambiguous: Dict[str, List[str]] = field(default_factory=dict)
    unknown: Set[str] = field(default_factory=set)

    def to_dict(self) -> Dict:
        return {
            "replaced": [
                {"from": src, "to": dst, "count": n} for (src, dst), n in sorted(self.replaced.items())
            ],
            "suspects": [{"token": t, "candidates": c} for t, c in sorted(self.suspects.items())],
            "ambiguous": [{"token": t, "candidates": c} for t, c in sorted(self.ambiguous.items())],
            "unknown": sorted(self.unknown),
        }


class NameNormalizer:
    def __init__(self, canonical_names: Iterable[str], aliases: Optional[Dict[str, str]] = None,
                 known_words: Iterable[str] = ()):
        """
        Args:
            canonical_names: HLTV 规范写法（选手、教练、战队）
            aliases: 别名 → 规范名（如 NAVI → Natus Vincere，小蜜蜂 → Vitality）
            known_words: 额外的已知英文词，不会出现在"未知词"提示里
        """
        self._by_key: Dict[str, Set[str]] = {}
        self.canonical: Set[str] = set()
        for name in canonical_names:
            self._add(name, name)

        self._plain_aliases: List[Tuple[str, str]] = []
        for alias, target in (aliases or {}).items():
            alias, target = alias.strip(), target.strip()
            if not alias or not target:
                continue
            self._add(target, target)
            if alias.isascii():
                self._add(alias, target)
            else:
                # 中文等非拉丁别名直接做字符串替换
                self._plain_aliases.append((alias, target))
        self._plain_aliases.sort(key=lambda item: len(item[0]), reverse=True)
        self._known = {w.lower() for w in known_words} | COMMON_WORDS

    def _add(self, text: str, target: str) -> None:
        key = name_key(text)
        if len(key) < 2:
            return
        self._by_key.setdefault(key, set()).add(target)
        self.canonical.add(target)

    def normalize(self, text: str) -> Tuple[str, NameReport]:
        report = NameReport()
        for alias, target in self._plain_aliases:
            count = text.count(alias)
            if count:
                text = text.replace(alias, target)
                report.replaced[(alias, target)] = report.replaced.get((alias, target), 0) + count

        tokens = list(LATIN_TOKEN.finditer(text))
        pieces: List[str] = []
        cursor = 0
        i = 0
        while i < len(tokens):
            hit = self._match_at(text, tokens, i)
            if hit is None:
                self._inspect_unmatched(tokens[i].group(), report)
                i += 1
                continue
            n, span_start, span_end, target = hit
            source = text[span_start:span_end]
            pieces.append(text[cursor:span_start])
            pieces.append(target)
            cursor = span_end
            if source != target:
                report.replaced[(source, target)] = report.replaced.get((source, target), 0) + 1
            i += n
        pieces.append(text[cursor:])
        return "".join(pieces), report

    def _match_at(self, text: str, tokens: List[re.Match], i: int):
        for n in range(min(MAX_SPAN, len(tokens) - i), 0, -1):
            group = tokens[i:i + n]
            if any(not JOINER.match(text[a.end():b.start()]) for a, b in zip(group, group[1:])):
                continue
            start, end = group[0].start(), group[-1].end()
            targets = self._by_key.get(name_key(text[start:end]))
            if not targets:
                continue
            if len(targets) > 1:
                # 匹配键冲突（两个规范名归一后相同）：不替换，交给人工
                return None
            return n, start, end, next(iter(targets))
        return None

    def _inspect_unmatched(self, token: str, report: NameReport) -> None:
        lowered = token.lower()
        key = name_key(token)
        targets = self._by_key.get(key)
        if targets and len(targets) > 1:
            report.ambiguous[token] = sorted(targets)
            return
        if lowered in self._known or token.isdigit() or len(key) < 3:
            return
        candidates = [
            name for name in sorted(self.canonical)
            if difflib.SequenceMatcher(None, key, name_key(name)).ratio() >= SUSPECT_RATIO
        ]
        if candidates:
            report.suspects[token] = candidates
        else:
            report.unknown.add(token)


def collect_canonical_names(context: Dict) -> List[str]:
    """从任务背景信息里收集规范名：战队名、阵容、教练、人工补充的名字。"""
    names: List[str] = []
    for team in context.get("teams") or []:
        if team.get("name"):
            names.append(team["name"])
        names.extend(p for p in team.get("players") or [] if p)
        coach = (team.get("stats") or {}).get("coach") or team.get("coach")
        if coach:
            names.append(coach)
    names.extend(n for n in context.get("extra_names") or [] if n)
    seen: Set[str] = set()
    return [n.strip() for n in names if n.strip() and not (n.strip() in seen or seen.add(n.strip()))]
