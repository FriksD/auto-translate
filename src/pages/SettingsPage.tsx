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

  type Section = 'hltv' | 'whisper' | 'diarization' | 'llm' | 'feishu'
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
      setFeishuResult('已发送测试消息，请到飞书查看')
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
            默认会弹出一个 Chrome 窗口访问 HLTV（空闲 10 分钟自动关闭）。遇到需要人工点击的 Cloudflare 验证时会发飞书提醒，在该窗口里点一下即可；无头模式下无法人工验证。
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
          <Field label="计算精度" hint="8GB 显存用 float16 即可；显存紧张选 int8_float16">
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
        <CardTitle>飞书</CardTitle>
        <div className="space-y-4">
          <Checkbox
            checked={settings.feishu.enabled}
            onChange={(enabled) => patch('feishu', { enabled })}
            label="初稿完成或处理失败时发送飞书消息"
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
          </div>
          <div className="flex items-center gap-3">
            <Button size="sm" variant="outline" onClick={testFeishu}>
              <Send className="mr-2 h-4 w-4" />
              发送测试消息
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
