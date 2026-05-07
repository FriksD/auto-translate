"""
LLM翻译服务 - 支持多种LLM后端
"""
import os
import logging
from typing import Dict, Any, Optional
import json
import requests

logger = logging.getLogger(__name__)


class LLMService:
    def __init__(self):
        self.providers = {
            'openai': self._call_openai,
            'bailian': self._call_bailian,
            'gemini': self._call_gemini,
            'relay_openai': self._call_relay_openai,
            'relay_anthropic': self._call_relay_anthropic,
        }

    @staticmethod
    def _extract_gemini_text(payload: Dict[str, Any], allow_empty: bool = False) -> str:
        """从 Gemini 响应中提取文本。"""
        candidates = payload.get("candidates") or []
        chunks = []
        for candidate in candidates:
            content = candidate.get("content") or {}
            parts = content.get("parts") or []
            for part in parts:
                if not isinstance(part, dict):
                    continue
                text = part.get("text")
                if isinstance(text, str) and text:
                    chunks.append(text)
        if chunks:
            return "".join(chunks)
        if allow_empty:
            return ""

        prompt_feedback = payload.get("promptFeedback") or {}
        block_reason = prompt_feedback.get("blockReason")
        if block_reason:
            raise RuntimeError(f"Gemini 请求被阻止: {block_reason}")
        raise RuntimeError("Gemini 返回为空，未生成可用文本")

    @staticmethod
    def _build_relay_headers(config: Dict[str, Any]) -> Dict[str, str]:
        """构建中转站请求头，允许从配置透传自定义 headers。"""
        headers: Dict[str, str] = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36"
        }
        custom_headers = config.get('headers')
        if isinstance(custom_headers, dict):
            for key, value in custom_headers.items():
                if isinstance(key, str) and isinstance(value, str) and value.strip():
                    headers[key] = value
        return headers

    @staticmethod
    def _should_fallback_to_chat(error: Exception) -> bool:
        """仅当 /responses 端点不被支持时才回退到 chat/completions。"""
        status_code = getattr(error, "status_code", None)
        if status_code in {404, 405, 501}:
            return True
        message = str(error).lower()
        unsupported_signals = (
            "responses",
            "not supported",
        )
        if all(token in message for token in unsupported_signals):
            return True
        return False

    def translate_to_news(
        self,
        transcript: Dict[str, Any],
        speakers: Dict[str, str],
        context: Optional[Dict[str, Any]] = None,
        config: Optional[Dict[str, Any]] = None
    ) -> str:
        """
        将转录内容翻译为新闻稿

        Args:
            transcript: 转录结果
            speakers: 说话人映射 {speaker_id: name}
            context: 背景信息 (HLTV数据等)
            config: LLM配置

        Returns:
            生成的新闻稿
        """
        config = config or {}
        provider = config.get('provider', 'openai')

        if provider not in self.providers:
            raise ValueError(f"不支持的LLM提供商: {provider}")

        # 构建提示词
        prompt_override = config.get('promptOverride')
        if isinstance(prompt_override, str) and prompt_override.strip():
            prompt = prompt_override
        else:
            prompt = self._build_prompt(transcript, speakers, context)

        # 调用对应的LLM
        return self.providers[provider](prompt, config)

    def build_prompt_preview(
        self,
        transcript: Dict[str, Any],
        speakers: Dict[str, str],
        context: Optional[Dict[str, Any]] = None
    ) -> str:
        """供前端预览/编辑后再提交的提示词。"""
        return self._build_prompt(transcript, speakers, context)

    def _build_prompt(
        self,
        transcript: Dict[str, Any],
        speakers: Dict[str, str],
        context: Optional[Dict[str, Any]] = None
    ) -> str:
        """构建翻译提示词"""

        # 格式化转录文本
        transcript_text = ""
        for segment in transcript.get('segments', []):
            speaker = segment.get('speaker', 'Unknown')
            speaker_name = speakers.get(speaker, speaker)
            text = segment.get('text', '')
            transcript_text += f"{speaker_name}: {text}\n"

        # ── 构建专有名词参考表 ────────────────────────────────────────────────
        separator = "━" * 62
        noun_table = ""
        team_rosters: Dict[str, list] = {}  # team_name → [player nicks]
        player_rows = []
        roster_rows = []
        coach_rows = []
        known_player_ids = set()

        if context and context.get('players'):
            for player in context['players']:
                nick = str(player.get('name', '')).strip()
                real_name = str(player.get('real_name', '')).strip()
                team = str(player.get('team', '')).strip()
                nation = str(player.get('nationality', '')).strip()
                stats = player.get('stats', {})
                rating = stats.get('rating', 'N/A')   # 小写 'rating'
                dpr = stats.get('dpr', 'N/A')
                kast = stats.get('kast_pct', 'N/A')

                if not nick:
                    continue
                nation_tag = f" [{nation}]" if nation else ""
                player_rows.append(f"  {nick:<16} → {real_name:<24} → {team}{nation_tag}")
                if rating != 'N/A':
                    player_rows.append(f"  {'':16}   {'':24}   Rating:{rating}  DPR:{dpr}  KAST:{kast}")

                known_player_ids.add(nick.casefold())
                if team:
                    team_rosters.setdefault(team, []).append(nick)

        team_entries = context.get('teams', []) if context else []
        seen_coaches = set()
        for team in team_entries:
            team_name = str(team.get('name', '')).strip()
            roster = team.get('players') or []
            coach = str((team.get('stats') or {}).get('coach', '')).strip()

            clean_roster = []
            for nick in roster:
                nick_text = str(nick).strip()
                if not nick_text:
                    continue
                if nick_text not in clean_roster:
                    clean_roster.append(nick_text)

            if team_name and clean_roster:
                existing_roster = team_rosters.setdefault(team_name, [])
                for nick in clean_roster:
                    if nick not in existing_roster:
                        existing_roster.append(nick)

            # 前端只添加战队时，将该战队五人阵容直接并入 noun table。
            # 如果战队返回了 player_details，使用其中的真实姓名
            player_details_map = {}
            for pd in (team.get('player_details') or []):
                if isinstance(pd, dict) and pd.get('name'):
                    player_details_map[pd['name'].casefold()] = pd

            for nick in clean_roster:
                nick_key = nick.casefold()
                if nick_key in known_player_ids:
                    continue
                known_player_ids.add(nick_key)

                # 从 player_details 获取真实姓名和国籍
                pd = player_details_map.get(nick_key, {})
                real_name = str(pd.get('real_name', '')).strip() if pd else ''
                nation = str(pd.get('nationality', '')).strip() if pd else ''
                stats = pd.get('stats', {}) if pd else {}

                nation_tag = f" [{nation}]" if nation else ""
                roster_rows.append(f"  {nick:<16} → {real_name:<24} → {team_name}{nation_tag}")

                # 如果有统计数据，添加第二行
                rating = stats.get('rating', 'N/A')
                if rating != 'N/A':
                    dpr = stats.get('dpr', 'N/A')
                    kast = stats.get('kast_pct', 'N/A')
                    roster_rows.append(f"  {'':16}   {'':24}   Rating:{rating}  DPR:{dpr}  KAST:{kast}")

            if team_name and coach:
                coach_key = (team_name.casefold(), coach.casefold())
                if coach_key not in seen_coaches:
                    seen_coaches.add(coach_key)
                    coach_rows.append(f"  {coach:<18} → {team_name}")

        if player_rows or roster_rows or coach_rows:
            noun_table += f"\n【专有名词参考表 — Whisper ASR错误修正，翻译前必须核对】\n"

        if player_rows or roster_rows:
            noun_table += f"\n选手游戏ID → 真实姓名 → 所属战队\n{separator}\n"
            noun_table += "\n".join(player_rows + roster_rows)
            noun_table += f"\n{separator}\n"

        if coach_rows:
            noun_table += f"\n战队教练 → 所属战队\n{separator}\n"
            noun_table += "\n".join(coach_rows)
            noun_table += f"\n{separator}\n"

        # ── 战队阵容 ──────────────────────────────────────────────────────────
        if team_entries:
            noun_table += "\n战队阵容（Roster）:\n"
            for team in team_entries:
                team_name = str(team.get('name', '')).strip()
                roster = team.get('players') or team_rosters.get(team_name, [])
                roster = [str(nick).strip() for nick in roster if str(nick).strip()]
                ranking = team.get('ranking', 'N/A')
                points = team.get('points', 'N/A')
                coach = (team.get('stats') or {}).get('coach', '')

                line = f"- {team_name} (排名 #{ranking}"
                if points != 'N/A':
                    line += f", {points}分"
                line += ")"
                if coach:
                    line += f", 教练: {coach}"
                noun_table += line + "\n"
                if roster:
                    noun_table += f"  阵容: {', '.join(roster)}\n"
                if team.get('recent_results'):
                    recent = ', '.join([r.get('result', '') for r in team['recent_results'][:3]])
                    noun_table += f"  近期战绩: {recent}\n"

        # ── 额外用户上下文 ────────────────────────────────────────────────────
        custom_context = ""
        if context and context.get('customContext'):
            custom_context = f"\n## 额外背景信息\n{context['customContext']}\n"

        prompt = f"""Background
用户需要将英文CS2赛事采访音频转录稿转化为符合中国电竞媒体风格的专业中文新闻稿。原始稿件由Whisper ASR生成，存在口语化表达、拼写错误、发音近似误识别等问题，且涉及大量CS2专业术语和即时赛事信息。目标读者为中国CS2玩家和电竞观众，要求译文兼具专业性与传播力，可直接发表于主流中文电竞媒体。
Attention
你深知Whisper对选手ID的识别极不可靠——你在翻译前必须先完成一轮专有名词清洗，将所有疑似选手名的词汇还原为参考表中的标准ID。
Skills

深度掌握CS2职业生态，包括现役选手、战队阵容、赛事体系、地图池及版本meta演变
精通CS2专业术语的中英文对照与本地化表达，熟悉中国大陆电竞社区的用语习惯
具备强大的ASR错误修复能力：通过发音相似性（音节数、元音结构、辅音骨架）、上下文战队归属、赛事背景三重验证，将Whisper误识别的词汇还原为正确选手ID
拥有优秀的中文新闻写作能力，能驾驭官方报道与生动叙事的双重风格
熟练运用HLTV.org等权威数据库进行选手ID、战队名称、赛事信息的核验

Goals

将英文CS2采访转录稿精准翻译为符合中国电竞媒体风格的专业中文新闻稿
全面清洗ASR误识别的选手ID：凡出现与参考表中选手真实姓名、ID发音近似的词汇，一律替换为标准ID
实现CS2专业术语的精准本地化，杜绝生硬直译与过时译法
完整保留原文的核心观点、战术细节、情感态度与语境层次
产出可直接发表于主流中文电竞媒体、无需二次修改的成品稿件

Constrains

严格采用对话格式输出，如果有一个人连续的多句话就合并成一段
所有专有名词必须与下方【专有名词参考表】完全一致，尤其是选手游戏ID的大小写、数字和特殊字符（如m0NESY中的数字0，ZywOo的大小写形式）
禁止逐字直译，必须在忠于原意的基础上进行符合中文行文习惯的语序调整与表达优化
不得遗漏任何核心观点、重要细节或隐含意义，包括说话人的语气与情感色彩
输出仅包含翻译后的中文新闻稿，禁止附带英文原文或任何解释性备注


【第一步：人名清洗协议——翻译前必须执行】

这是整个工作流中优先级最高的步骤。在进行任何翻译之前，必须完成以下人名清洗。

清洗规则
规则1：真实姓名 → 标准ID
转录稿中若出现选手的真实姓名（全名或姓氏），必须替换为其游戏ID。

示例：William / Merriman / William Merriman → mezii
示例：Ilya / Osipov / Ilya Osipov → m0nesy

规则2：发音近似词 → 标准ID（核心规则）
Whisper会将选手ID识别为发音相似的普通英文词汇。对照下方【发音陷阱速查表】，将这些词汇替换回正确ID。
规则3：大小写/数字变体 → 标准ID
即使拼写接近但大小写或数字不符，也必须统一为参考表中的精确形式。

规则4：上下文辅助验证
当某个词汇处于以下语境时，优先考虑其为选手ID误识别：

出现在"he/his/him plays..."、"...said"、"...told us"等主语位置
与战队名（Vitality、Falcons等）同句出现
与CS2战术/比赛行为（clutch、entry、rifle、AWP等）搭配


Workflow

【人名清洗】 通读全文，对照【发音陷阱速查表】和【专有名词参考表】，用"查找可疑词→音节比对→上下文验证→替换为标准ID"的流程，完成所有选手ID的清洗。对每一个疑似人名的词汇保持最高警惕。
【专有名词核查】 逐一核查战队名称、赛事名称、地图名称，通过HLTV.org等权威源确认标准写法
【首轮翻译】 将清洗后的英文内容转化为中文，同步完成CS2专业术语的本地化转换
【译文润色】 调整语序与表达方式，精准还原说话人的语气、情感与观点态度
【终稿校对】 最终逐一确认所有选手ID与参考表一致，格式规范，达到直接发表标准

OutputFormat

严格遵循对话结构，其中不要有任何md格式的符号，如果有一个人连续的多句话就合并成一段
全文为纯中文内容，不包含任何英文原文、译者注释或辅助说明
段落层次分明，长回答按逻辑意群适当分段，提升阅读体验

{noun_table}{custom_context}
采访/访谈内容（原始Whisper转录，可能含识别错误）
{transcript_text}
"""
        logger.info("Noun table debug output:\n%s", noun_table if noun_table else "(empty)")
        return prompt

    def _call_openai(self, prompt: str, config: Dict[str, Any]) -> str:
        """调用OpenAI API"""
        from openai import OpenAI

        client = OpenAI(
            api_key=config.get('apiKey') or os.environ.get('OPENAI_API_KEY'),
            base_url=config.get('baseUrl')
        )

        model = config.get('model', 'gpt-4-turbo-preview')

        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": "你是一位深耕CS2电竞领域多年的资深新闻主编，对职业生态、选手动态、战术体系有深刻理解。你的翻译不仅是语言转换，更是专业内容的再创作。"},
                {"role": "user", "content": prompt}
            ],
            temperature=0.7,
            max_tokens=4000
        )

        return response.choices[0].message.content



    def _call_bailian(self, prompt: str, config: Dict[str, Any]) -> str:
        """调用阿里百炼API (OpenAI兼容格式)"""
        from openai import OpenAI

        api_key = config.get('apiKey') or os.environ.get('BAILIAN_API_KEY')
        model = config.get('model', 'kimi-k2.5')

        # 阿里百炼使用OpenAI兼容接口
        client = OpenAI(
            api_key=api_key,
            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1"
        )

        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": "你是一位专业的CS电竞新闻编辑。"},
                {"role": "user", "content": prompt}
            ],
            temperature=0.7,
            max_tokens=4000
        )

        return response.choices[0].message.content

    def _call_gemini(self, prompt: str, config: Dict[str, Any]) -> str:
        """调用 Gemini 官方 SDK。"""
        from google import genai
        from google.genai import types

        api_key = config.get('apiKey') or os.environ.get('GEMINI_API_KEY')
        if not api_key:
            raise ValueError("未配置 Gemini API Key")

        model = config.get('model', 'gemini-2.5-flash')

        client = genai.Client(api_key=api_key)

        response = client.models.generate_content(
            model=model,
            contents=prompt,
            config=types.GenerateContentConfig(
                system_instruction="你是一位深耕CS2电竞领域多年的资深新闻主编，对职业生态、选手动态、战术体系有深刻理解。你的翻译不仅是语言转换，更是专业内容的再创作。",
                temperature=0.7,
                max_output_tokens=4000
            )
        )

        return response.text

    def stream_translate(
        self,
        transcript: Dict[str, Any],
        speakers: Dict[str, str],
        context: Optional[Dict[str, Any]] = None,
        config: Optional[Dict[str, Any]] = None
    ):
        """
        流式翻译（用于实时显示）

        Yields:
            生成的文本片段
        """
        config = config or {}
        provider = config.get('provider', 'openai')

        prompt_override = config.get('promptOverride')
        if isinstance(prompt_override, str) and prompt_override.strip():
            prompt = prompt_override
        else:
            prompt = self._build_prompt(transcript, speakers, context)

        if provider == 'openai':
            yield from self._stream_openai(prompt, config)
        elif provider == 'bailian':
            yield from self._stream_bailian(prompt, config)
        elif provider == 'gemini':
            yield from self._stream_gemini(prompt, config)
        elif provider == 'relay_openai':
            yield from self._stream_relay_openai(prompt, config)
        elif provider == 'relay_anthropic':
            yield from self._stream_relay_anthropic(prompt, config)
        else:
            # 其他提供商暂不支持流式
            yield self.providers[provider](prompt, config)

    def _stream_openai(self, prompt: str, config: Dict[str, Any]):
        """OpenAI流式调用"""
        from openai import OpenAI

        client = OpenAI(
            api_key=config.get('apiKey') or os.environ.get('OPENAI_API_KEY'),
            base_url=config.get('baseUrl')
        )

        model = config.get('model', 'gpt-4-turbo-preview')

        stream = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": "你是一位专业的CS电竞新闻编辑。"},
                {"role": "user", "content": prompt}
            ],
            temperature=0.7,
            max_tokens=4000,
            stream=True
        )

        for chunk in stream:
            if chunk.choices[0].delta.content:
                yield chunk.choices[0].delta.content

    def _call_relay_openai(self, prompt: str, config: Dict[str, Any]) -> str:
        """调用OpenAI格式中转站（使用 /v1/responses 接口）"""
        from openai import OpenAI

        client = OpenAI(
            api_key=config.get('apiKey', 'sk-placeholder'),
            base_url=config.get('baseUrl', 'https://api.openai.com/v1'),
            default_headers=self._build_relay_headers(config),
        )
        model = config.get('model', 'gpt-4o')

        try:
            response = client.responses.create(
                model=model,
                instructions="你是一位深耕CS2电竞领域多年的资深新闻主编，对职业生态、选手动态、战术体系有深刻理解。你的翻译不仅是语言转换，更是专业内容的再创作。",
                input=prompt,
                temperature=0.7,
                max_output_tokens=4000,
            )
        except Exception as error:
            if not self._should_fallback_to_chat(error):
                raise
            logger.warning("relay_openai /responses 不可用，回退 /chat/completions: %s", error)
            completion = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": "你是一位深耕CS2电竞领域多年的资深新闻主编，对职业生态、选手动态、战术体系有深刻理解。你的翻译不仅是语言转换，更是专业内容的再创作。"},
                    {"role": "user", "content": prompt}
                ],
                temperature=0.7,
                max_tokens=4000
            )
            return completion.choices[0].message.content or ""
        if isinstance(response, str):
            return response
        if hasattr(response, 'output_text'):
            return response.output_text
        # fallback: chat completions format
        return response.choices[0].message.content

    def _stream_relay_openai(self, prompt: str, config: Dict[str, Any]):
        """OpenAI格式中转站流式调用（使用 /v1/responses 接口）"""
        from openai import OpenAI

        client = OpenAI(
            api_key=config.get('apiKey', 'sk-placeholder'),
            base_url=config.get('baseUrl', 'https://api.openai.com/v1'),
            default_headers=self._build_relay_headers(config),
        )
        model = config.get('model', 'gpt-4o')

        try:
            with client.responses.stream(
                model=model,
                instructions="你是一位深耕CS2电竞领域多年的资深新闻主编，对职业生态、选手动态、战术体系有深刻理解。你的翻译不仅是语言转换，更是专业内容的再创作。",
                input=prompt,
                temperature=0.7,
                max_output_tokens=4000,
            ) as stream:
                for text in stream.text_stream:
                    yield text
        except Exception as error:
            if not self._should_fallback_to_chat(error):
                raise
            logger.warning("relay_openai /responses 流式不可用，回退 chat.completions 流式: %s", error)
            stream = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": "你是一位深耕CS2电竞领域多年的资深新闻主编，对职业生态、选手动态、战术体系有深刻理解。你的翻译不仅是语言转换，更是专业内容的再创作。"},
                    {"role": "user", "content": prompt}
                ],
                temperature=0.7,
                max_tokens=4000,
                stream=True
            )
            for chunk in stream:
                if chunk.choices and chunk.choices[0].delta and chunk.choices[0].delta.content:
                    yield chunk.choices[0].delta.content

    def _call_relay_anthropic(self, prompt: str, config: Dict[str, Any]) -> str:
        """调用Anthropic格式中转站"""
        import anthropic

        client = anthropic.Anthropic(
            api_key=config.get('apiKey', ''),
            base_url=config.get('baseUrl', 'https://api.anthropic.com'),
            default_headers=self._build_relay_headers(config),
        )
        model = config.get('model', 'claude-sonnet-4-6')

        message = client.messages.create(
            model=model,
            max_tokens=4000,
            system="你是一位深耕CS2电竞领域多年的资深新闻主编，对职业生态、选手动态、战术体系有深刻理解。你的翻译不仅是语言转换，更是专业内容的再创作。",
            messages=[{"role": "user", "content": prompt}]
        )
        return message.content[0].text

    def _stream_relay_anthropic(self, prompt: str, config: Dict[str, Any]):
        """Anthropic格式中转站流式调用"""
        import anthropic

        client = anthropic.Anthropic(
            api_key=config.get('apiKey', ''),
            base_url=config.get('baseUrl', 'https://api.anthropic.com'),
            default_headers=self._build_relay_headers(config),
        )
        model = config.get('model', 'claude-sonnet-4-6')

        with client.messages.stream(
            model=model,
            max_tokens=4000,
            system="你是一位深耕CS2电竞领域多年的资深新闻主编，对职业生态、选手动态、战术体系有深刻理解。你的翻译不仅是语言转换，更是专业内容的再创作。",
            messages=[{"role": "user", "content": prompt}]
        ) as stream:
            for text in stream.text_stream:
                yield text

    def _stream_bailian(self, prompt: str, config: Dict[str, Any]):
        """阿里百炼流式调用"""
        from openai import OpenAI

        api_key = config.get('apiKey') or os.environ.get('BAILIAN_API_KEY')
        model = config.get('model', 'qwen-turbo')

        client = OpenAI(
            api_key=api_key,
            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1"
        )

        stream = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": "你是一位专业的CS电竞新闻编辑。"},
                {"role": "user", "content": prompt}
            ],
            temperature=0.7,
            max_tokens=4000,
            stream=True
        )

        for chunk in stream:
            if chunk.choices[0].delta.content:
                yield chunk.choices[0].delta.content

    def _stream_gemini(self, prompt: str, config: Dict[str, Any]):
        """Gemini 官方 SDK 流式调用。"""
        from google import genai
        from google.genai import types

        api_key = config.get('apiKey') or os.environ.get('GEMINI_API_KEY')
        if not api_key:
            raise ValueError("未配置 Gemini API Key")

        model = config.get('model', 'gemini-2.5-flash')

        client = genai.Client(api_key=api_key)

        for chunk in client.models.generate_content_stream(
            model=model,
            contents=prompt,
            config=types.GenerateContentConfig(
                system_instruction="你是一位深耕CS2电竞领域多年的资深新闻主编，对职业生态、选手动态、战术体系有深刻理解。你的翻译不仅是语言转换，更是专业内容的再创作。",
                temperature=0.7,
                max_output_tokens=4000
            )
        ):
            if chunk.text:
                yield chunk.text
