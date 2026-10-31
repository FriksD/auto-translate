import { useEffect, useState } from 'react'
import { Loader2, Save, Send } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Textarea } from '@/components/ui/textarea'
import { Card, CardTitle, Checkbox, ErrorText, Field, Select } from '@/components/ui/form'
import { ProviderList, SecretInput } from '@/components/settings/ProviderList'
import { api, LlmStep, Settings } from '@/lib/api'

function StepPicker({
  label,
  hint,
  step,
  settings,
  onChange,
}: {
  label: string
  hint: string
  step: LlmStep
  settings: Settings
  onChange: (step: LlmStep) => void
}) {
  const provider = settings.llm.providers.find((p) => p.id === step.provider_id)
  return (
    <div className="grid gap-3 md:grid-cols-2">
      <Field label={label} hint={hint}>
        <Select value={step.provider_id} onChange={(e) => onChange({ ...step, provider_id: e.target.value })}>
          <option value="">（未选择）</option>
          {settings.llm.providers.map((p) => (
            <option key={p.id} value={p.id}>
              {p.name}
            </option>
          ))}
        </Select>
      </Field>
      <Field label="模型" hint="留空则使用供应商的默认模型">
        <Input
          value={step.model}
          placeholder={provider?.model || ''}
          onChange={(e) => onChange({ ...step, model: e.target.value })}
        />
      </Field>
    </div>
  )
}

function NumberField({ label, value, onChange }: { label: string; value: number; onChange: (value: number) => void }) {
  return (
    <Field label={label}>
      <Input type="number" min="0" value={value} onChange={(e) => onChange(Number(e.target.value))} />
    </Field>
  )
}

export function SettingsPage() {
  const [settings, setSettings] = useState<Settings | null>(null)
  const [saving, setSaving] = useState(false)
  const [message, setMessage] = useState('')
  const [error, setError] = useState('')
  const [feishuResult, setFeishuResult] = useState('')

  useEffect(() => {
    api.getSettings().then(setSettings).catch((e) => setError(e.message))
  }, [])

  if (!settings) {
    return error ? <ErrorText>{error}</ErrorText> : <Loader2 className="mx-auto mt-20 h-6 w-6 animate-spin" />
  }

  type Section = 'hltv' | 'whisper' | 'diarization' | 'llm' | 'live' | 'feishu'
  const patch = <K extends Section>(key: K, value: Partial<Settings[K]>) =>
    setSettings({ ...settings, [key]: { ...settings[key], ...value } })

  const save = async () => {
    setSaving(true)
    setError('')
    try {
      setSettings(await api.saveSettings(settings))
      setMessage('已保存')
      setTimeout(() => setMessage(''), 2000)
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setSaving(false)
    }
  }

  const testFeishu = async () => {
    setFeishuResult('发送中...')
    try {
      await api.testFeishu(settings.feishu)
      setFeishuResult('已发送测试卡片，请到飞书查看；点卡片上的按钮会提示回调是否连通')
    } catch (e) {
      setFeishuResult(`失败：${(e as Error).message}`)
    }
  }

  return (
    <div className="space-y-6 pb-20">
      <h1 className="text-xl font-semibold">设置</h1>

      <Card>
        <CardTitle>网络</CardTitle>
        <div className="grid gap-4 md:grid-cols-2">
          <Field label="代理" hint="HLTV、视频下载、LLM（可按供应商关闭）共用；留空为直连。飞书始终直连。">
            <Input value={settings.proxy} onChange={(e) => setSettings({ ...settings, proxy: e.target.value })} placeholder="http://127.0.0.1:7897" />
          </Field>
          <Field label="ffmpeg 路径" hint="留空自动查找 PATH">
            <Input value={settings.ffmpeg_path} onChange={(e) => setSettings({ ...settings, ffmpeg_path: e.target.value })} />
          </Field>
        </div>
        <div className="mt-4 space-y-1">
          <Checkbox
            checked={settings.hltv.headless}
            onChange={(headless) => patch('hltv', { headless })}
            label="HLTV 浏览器使用无头模式"
          />
          <p className="text-xs text-muted-foreground">
            默认会弹出一个 Chrome 窗口访问 HLTV（空闲 10 分钟自动关闭），Cloudflare 验证一般 20 秒左右自动通过；偶尔需要人工点击时会发飞书提醒。无头模式下无法人工验证。
          </p>
        </div>
      </Card>

      <Card>
        <CardTitle>LLM</CardTitle>
        <div className="space-y-6">
          <ProviderList
            providers={settings.llm.providers}
            onChange={(providers) => patch('llm', { providers })}
          />
          <div className="space-y-4 border-t pt-5">
            <StepPicker
              label="第一步：英文清洗"
              hint="修正转写错误、统一选手名、整理成英文问答稿"
              step={settings.llm.clean}
              settings={settings}
              onChange={(clean) => patch('llm', { clean })}
            />
            <StepPicker
              label="第二步：中文成稿"
              hint="翻译润色为中文 Q/A 稿，建议用最强的模型"
              step={settings.llm.write}
              settings={settings}
              onChange={(write) => patch('llm', { write })}
            />
            <StepPicker
              label="直播采访定位"
              hint="录制期间每 30 秒判断一次采访起止，响应越快越早出稿；不选则沿用英文清洗的设置"
              step={settings.llm.detect}
              settings={settings}
              onChange={(detect) => patch('llm', { detect })}
            />
            <Field label="Temperature" className="max-w-[200px]">
              <Input
                type="number"
                step="0.1"
                min="0"
                max="1.5"
                value={settings.llm.temperature}
                onChange={(e) => patch('llm', { temperature: Number(e.target.value) })}
              />
            </Field>
            <Field label="补充写稿要求" hint="对所有稿件生效，会附加在中文成稿的提示词末尾">
              <Textarea
                value={settings.llm.extra_instructions}
                onChange={(e) => patch('llm', { extra_instructions: e.target.value })}
              />
            </Field>
          </div>
        </div>
      </Card>

      <Card>
        <CardTitle>语音转写</CardTitle>
        <div className="grid gap-4 md:grid-cols-2">
          <Field label="Whisper 模型" hint="本地模型目录，或 large-v3 / large-v3-turbo 等模型名（首次会下载）">
            <Input value={settings.whisper.model} onChange={(e) => patch('whisper', { model: e.target.value })} />
          </Field>
          <Field label="语言" hint="留空自动检测；纯英文采访可填 en">
            <Input value={settings.whisper.language} onChange={(e) => patch('whisper', { language: e.target.value })} />
          </Field>
          <Field label="设备">
            <Select value={settings.whisper.device} onChange={(e) => patch('whisper', { device: e.target.value })}>
              <option value="cuda">GPU (cuda)</option>
              <option value="cpu">CPU</option>
            </Select>
          </Field>
          <Field label="计算精度" hint="推荐 int8_float16：显存占用少一半，速度和效果基本一样；float16 在 8GB 显存上容易挤爆变慢">
            <Select value={settings.whisper.compute_type} onChange={(e) => patch('whisper', { compute_type: e.target.value })}>
              <option value="float16">float16</option>
              <option value="int8_float16">int8_float16</option>
              <option value="int8">int8（CPU）</option>
            </Select>
          </Field>
          <Field label="Beam size" hint="越大越准但越慢，1～5">
            <Input
              type="number"
              min="1"
              max="10"
              value={settings.whisper.beam_size}
              onChange={(e) => patch('whisper', { beam_size: Number(e.target.value) })}
            />
          </Field>
        </div>
        <div className="mt-5 space-y-4 border-t pt-5">
          <Checkbox
            checked={settings.diarization.enabled}
            onChange={(enabled) => patch('diarization', { enabled })}
            label="启用说话人分离（pyannote，帮助区分主持人和选手）"
          />
          {settings.diarization.enabled && (
            <div className="grid gap-4 md:grid-cols-2">
              <Field label="本地模型目录" hint="推荐，免认证">
                <Input value={settings.diarization.model_path} onChange={(e) => patch('diarization', { model_path: e.target.value })} />
              </Field>
              <Field label="HuggingFace Token" hint="仅使用远程模型时需要">
                <SecretInput value={settings.diarization.hf_token} onChange={(hf_token) => patch('diarization', { hf_token })} />
              </Field>
            </div>
          )}
        </div>
      </Card>

      <Card>
        <CardTitle>直播监视与录制</CardTitle>
        <div className="space-y-4">
          <p className="text-xs text-muted-foreground">
            决胜图（任一方再赢一张图就赢下整场）按领先方回合数提高检查频率，达到「开始录制」的分数时开始录官方直播；
            数据源没有回合比分时（bo3.gg 的普通比赛），决胜图一开打就开始录。
          </p>
          <Field
            label="比分数据源"
            hint="前一个读取失败时自动换下一个。bo3.gg 是公开接口，不需要浏览器；HLTV 有实时回合比分，但偶尔要过 Cloudflare 验证。"
            className="max-w-md"
          >
            <Select value={settings.live.sources.join(',')} onChange={(e) => patch('live', { sources: e.target.value.split(',') })}>
              <option value="bo3,hltv">bo3.gg 优先，失败时用 HLTV</option>
              <option value="hltv,bo3">HLTV 优先，失败时用 bo3.gg</option>
              <option value="bo3">只用 bo3.gg</option>
              <option value="hltv">只用 HLTV</option>
            </Select>
          </Field>
          <div className="grid gap-4 md:grid-cols-4">
            <NumberField label="开赛前开始监视（分钟）" value={settings.live.pre_match_minutes} onChange={(pre_match_minutes) => patch('live', { pre_match_minutes })} />
            <NumberField label="领先方达到几分改为中频" value={settings.live.mid_at} onChange={(mid_at) => patch('live', { mid_at })} />
            <NumberField label="领先方达到几分改为高频" value={settings.live.late_at} onChange={(late_at) => patch('live', { late_at })} />
            <NumberField label="领先方达到几分开始录制" value={settings.live.record_at} onChange={(record_at) => patch('live', { record_at })} />
          </div>
          <NumberField
            label="没有回合比分时，决胜图开打几分钟后录制"
            value={settings.live.no_score_record_minutes}
            onChange={(no_score_record_minutes) => patch('live', { no_score_record_minutes })}
          />
          <div className="grid gap-4 md:grid-cols-4">
            {(['idle', 'mid', 'late', 'recording'] as const).map((key) => (
              <NumberField
                key={key}
                label={{ idle: '低频间隔（秒）', mid: '中频间隔（秒）', late: '高频/加时间隔（秒）', recording: '录制中间隔（秒）' }[key]}
                value={settings.live.intervals[key]}
                onChange={(v) => patch('live', { intervals: { ...settings.live.intervals, [key]: v } })}
              />
            ))}
          </div>
          <div className="grid gap-4 md:grid-cols-4">
            <NumberField label="录音分段（秒）" value={settings.live.chunk_seconds} onChange={(chunk_seconds) => patch('live', { chunk_seconds })} />
            <NumberField label="抽帧间隔（秒，0 = 只录音）" value={settings.live.frame_interval} onChange={(frame_interval) => patch('live', { frame_interval })} />
            <NumberField label="采访判断间隔（秒）" value={settings.live.detect_interval} onChange={(detect_interval) => patch('live', { detect_interval })} />
            <NumberField label="采访最长（分钟）" value={settings.live.max_interview_minutes} onChange={(max_interview_minutes) => patch('live', { max_interview_minutes })} />
            <NumberField label="截取前后余量（秒）" value={settings.live.padding_seconds} onChange={(padding_seconds) => patch('live', { padding_seconds })} />
          </div>
          <div className="grid gap-4 md:grid-cols-4">
            <NumberField label="赛后多久无采访就提醒（分钟）" value={settings.live.alert_no_interview_minutes} onChange={(alert_no_interview_minutes) => patch('live', { alert_no_interview_minutes })} />
            <NumberField label="赛后多久无采访就停止（分钟）" value={settings.live.max_post_match_minutes} onChange={(max_post_match_minutes) => patch('live', { max_post_match_minutes })} />
            <NumberField label="采访结束后多久没出稿就提醒（分钟）" value={settings.live.draft_deadline_minutes} onChange={(draft_deadline_minutes) => patch('live', { draft_deadline_minutes })} />
          </div>
          <p className="text-xs text-muted-foreground">
            时效：录音分段和判断间隔越短，采访结束后越快被发现（默认 30 秒 / 30 秒，实测约 2 分钟内截取），后处理约 1～3 分钟。
          </p>
        </div>
      </Card>

      <Card>
        <CardTitle>飞书</CardTitle>
        <div className="space-y-4">
          <Checkbox
            checked={settings.feishu.enabled}
            onChange={(enabled) => patch('feishu', { enabled })}
            label="在飞书群里为每个任务发一张进度卡片，需要处理的事在卡片下提醒"
          />
          <div className="grid gap-4 md:grid-cols-2">
            <Field label="App ID">
              <Input value={settings.feishu.app_id} onChange={(e) => patch('feishu', { app_id: e.target.value })} />
            </Field>
            <Field label="App Secret">
              <SecretInput value={settings.feishu.app_secret} onChange={(app_secret) => patch('feishu', { app_secret })} />
            </Field>
            <Field label="发送给">
              <Select
                value={settings.feishu.receive_id_type}
                onChange={(e) => patch('feishu', { receive_id_type: e.target.value as Settings['feishu']['receive_id_type'] })}
              >
                <option value="chat_id">群聊（chat_id）</option>
                <option value="open_id">个人（open_id）</option>
              </Select>
            </Field>
            <Field label={settings.feishu.receive_id_type === 'chat_id' ? '群 chat_id' : '用户 open_id'} hint="应用需要先被拉进群，或对该用户可见">
              <Input value={settings.feishu.receive_id} onChange={(e) => patch('feishu', { receive_id: e.target.value })} />
            </Field>
            <Field label="在线精校地址" hint="内网穿透的公网地址，如 https://xxx.ngrok-free.dev；留空则卡片上没有在线精校按钮">
              <Input
                value={settings.feishu.public_url}
                placeholder="https://"
                onChange={(e) => patch('feishu', { public_url: e.target.value })}
              />
            </Field>
          </div>
          <div className="flex items-center gap-3">
            <Button size="sm" variant="outline" onClick={testFeishu}>
              <Send className="mr-2 h-4 w-4" />
              发送测试卡片
            </Button>
            {feishuResult && <span className="text-sm text-muted-foreground">{feishuResult}</span>}
          </div>
        </div>
      </Card>

      <div className="fixed inset-x-0 bottom-0 border-t bg-background/95 backdrop-blur">
        <div className="mx-auto flex max-w-6xl items-center gap-3 px-6 py-3">
          <Button onClick={save} disabled={saving}>
            {saving ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : <Save className="mr-2 h-4 w-4" />}
            保存设置
          </Button>
          {message && <span className="text-sm text-emerald-700">{message}</span>}
          <ErrorText>{error}</ErrorText>
        </div>
      </div>
    </div>
  )
}
