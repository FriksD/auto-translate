"""
专有名词规范化：保证稿件中的选手名、战队名与 HLTV 写法完全一致。

逐级匹配，越往后越依赖"推断"：
1. exact     匹配键相同：小写 + leet 数字归一 + 去符号（m0NESY / monesy / Mo Nesy）
2. phonetic  发音键相同：在匹配键基础上合并重复字母、统一近音字母（Monessy → monesy）
3. skeleton  辅音骨架相同：去掉元音后一致且长度相近（Manasi ↔ m0NESY 都是 m-n-s）
4. llm       以上都不命中、但疑似人名的词，结合所在句子交给 LLM 判断（见 stages.resolve_names）

只对中文稿件做替换：中文稿里的拉丁字母词几乎都是专有名词，
误伤普通英文单词（如 s1mple ↔ simple）的风险很低。所有推断出的替换都会写进报告，供人工复核。
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
SUSPECT_RATIO = 0.7
TIERS = ("exact", "phonetic", "skeleton")

# 近音替换，按顺序应用在匹配键上
PHONETIC_RULES = [("ph", "f"), ("ck", "k"), ("x", "ks"), ("c", "k"), ("q", "k"), ("z", "s"), ("y", "i"), ("w", "v")]
VOWELS = re.compile(r"[aeiou]")

# 稿件中常见、无需提示的英文词（大小写不敏感）
COMMON_WORDS = {
    "cs", "cs2", "csgo", "hltv", "major", "mvp", "evp", "igl", "awp", "awper", "ak", "m4", "ct", "t",
    "bo1", "bo3", "bo5", "gg", "ot", "eco", "clutch", "ace", "entry", "rifler", "lurker", "anchor",
    "dust2", "mirage", "inferno", "nuke", "overpass", "ancient", "anubis", "vertigo", "train", "cache",
    "iem", "esl", "blast", "pgl", "pro", "league", "cologne", "katowice", "q", "a", "vs",
}


def name_key(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower().translate(LEET_MAP))


def phonetic_key(text: str) -> str:
    key = re.sub(r"(.)\1+", r"\1", name_key(text))
    for src, dst in PHONETIC_RULES:
        key = key.replace(src, dst)
    return re.sub(r"(.)\1+", r"\1", key)


def skeleton_key(text: str) -> str:
    """辅音骨架：保留首字母，去掉其余元音。太短的骨架区分度不够，返回空串。"""
    key = phonetic_key(text)
    skeleton = key[:1] + VOWELS.sub("", key[1:])
    return skeleton if len(skeleton) >= 3 and len(key) >= 4 else ""


KEY_FUNCS = {"exact": name_key, "phonetic": phonetic_key, "skeleton": skeleton_key}


@dataclass
class NameReport:
    # (原文, 规范名, 方式) → 次数
    replaced: Dict[Tuple[str, str, str], int] = field(default_factory=dict)
    suspects: Dict[str, List[str]] = field(default_factory=dict)
    ambiguous: Dict[str, List[str]] = field(default_factory=dict)
    unknown: Set[str] = field(default_factory=set)

    def add_replacement(self, source: str, target: str, method: str, count: int = 1) -> None:
        key = (source, target, method)
        self.replaced[key] = self.replaced.get(key, 0) + count

    def to_dict(self) -> Dict:
        return {
            "replaced": [
                {"from": src, "to": dst, "method": method, "count": n}
                for (src, dst, method), n in sorted(self.replaced.items())
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
            known_words: 额外的已知英文词，不参与推断，也不会出现在"未知词"提示里
        """
        self._index: Dict[str, Dict[str, Set[str]]] = {tier: {} for tier in TIERS}
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
        if len(name_key(text)) < 2:
            return
        self.canonical.add(target)
        for tier in TIERS:
            key = KEY_FUNCS[tier](text)
            if key:
                self._index[tier].setdefault(key, set()).add(target)

    def normalize(self, text: str) -> Tuple[str, NameReport]:
        report = NameReport()
        for alias, target in self._plain_aliases:
            count = text.count(alias)
            if count:
                text = text.replace(alias, target)
                report.add_replacement(alias, target, "alias", count)

        tokens = list(LATIN_TOKEN.finditer(text))
        pieces: List[str] = []
        cursor = 0
        i = 0
        while i < len(tokens):
            hit = self._match_at(text, tokens, i, report)
            if hit is None:
                self._inspect_unmatched(tokens[i].group(), report)
                i += 1
                continue
            n, span_start, span_end, target, method = hit
            source = text[span_start:span_end]
            pieces.append(text[cursor:span_start])
            pieces.append(target)
            cursor = span_end
            if source != target:
                report.add_replacement(source, target, method)
            i += n
        pieces.append(text[cursor:])
        return "".join(pieces), report

    def _spans(self, text: str, tokens: List[re.Match], i: int):
        """从第 i 个词开始的候选片段（长的优先）。"""
        for n in range(min(MAX_SPAN, len(tokens) - i), 0, -1):
            group = tokens[i:i + n]
            if any(not JOINER.match(text[a.end():b.start()]) for a, b in zip(group, group[1:])):
                continue
            yield n, group[0].start(), group[-1].end()

    def _match_at(self, text: str, tokens: List[re.Match], i: int, report: NameReport):
        spans = list(self._spans(text, tokens, i))
        for tier in TIERS:
            for n, start, end in spans:
                span_text = text[start:end]
                # 已知的普通英文词 / 术语只做精确匹配，不做发音推断
                if tier != "exact" and (n == 1 and span_text.lower() in self._known):
                    continue
                key = KEY_FUNCS[tier](span_text)
                targets = self._index[tier].get(key) if key else None
                if not targets:
                    continue
                if len(targets) > 1:
                    # 多个规范名归一后相同：不替换，交给人工
                    report.ambiguous[span_text] = sorted(targets)
                    return None
                return n, start, end, next(iter(targets)), tier
        return None

    def _inspect_unmatched(self, token: str, report: NameReport) -> None:
        if token in report.ambiguous:
            return
        key = phonetic_key(token)
        if token.lower() in self._known or token.isdigit() or len(key) < 3:
            return
        candidates = [
            name for name in sorted(self.canonical)
            if difflib.SequenceMatcher(None, key, phonetic_key(name)).ratio() >= SUSPECT_RATIO
        ]
        if candidates:
            report.suspects[token] = candidates
        else:
            report.unknown.add(token)


def replace_tokens(text: str, mapping: Dict[str, str]) -> Tuple[str, Dict[str, int]]:
    """把整词出现的 token 替换为目标写法，返回新文本和每个 token 的替换次数。"""
    counts: Dict[str, int] = {}
    for token, target in sorted(mapping.items(), key=lambda item: len(item[0]), reverse=True):
        pattern = re.compile(rf"(?<![A-Za-z0-9]){re.escape(token)}(?![A-Za-z0-9])")
        text, n = pattern.subn(target, text)
        if n:
            counts[token] = n
    return text, counts


def token_contexts(text: str, tokens: Iterable[str], width: int = 40) -> Dict[str, List[str]]:
    """每个 token 在文中出现位置附近的片段，供 LLM 结合上下文判断。"""
    contexts: Dict[str, List[str]] = {}
    for token in tokens:
        pattern = re.compile(rf"(?<![A-Za-z0-9]){re.escape(token)}(?![A-Za-z0-9])")
        snippets = []
        for match in pattern.finditer(text):
            start, end = max(0, match.start() - width), min(len(text), match.end() + width)
            snippets.append(text[start:end].replace("\n", " "))
            if len(snippets) >= 3:
                break
        if snippets:
            contexts[token] = snippets
    return contexts


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
