import { useState } from 'react'
import { Eye, EyeOff, Loader2, Plus, Trash2, Zap } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Checkbox, Field, Select } from '@/components/ui/form'
import { api, LlmProvider } from '@/lib/api'

function SecretInput({ value, onChange }: { value: string; onChange: (v: string) => void }) {
  const [show, setShow] = useState(false)
  return (
    <div className="relative">
      <Input type={show ? 'text' : 'password'} value={value} onChange={(e) => onChange(e.target.value)} className="pr-10" />
      <button
        type="button"
        className="absolute right-3 top-1/2 -translate-y-1/2 text-muted-foreground"
        onClick={() => setShow((v) => !v)}
      >
        {show ? <EyeOff className="h-4 w-4" /> : <Eye className="h-4 w-4" />}
      </button>
    </div>
  )
}

function ProviderRow({
  provider,
  onChange,
  onRemove,
}: {
  provider: LlmProvider
  onChange: (p: LlmProvider) => void
  onRemove: () => void
}) {
  const [testing, setTesting] = useState(false)
  const [result, setResult] = useState<{ ok: boolean; text: string } | null>(null)
  const set = <K extends keyof LlmProvider>(key: K, value: LlmProvider[K]) => onChange({ ...provider, [key]: value })

  const test = async () => {
    setTesting(true)
    setResult(null)
    try {
      const { reply } = await api.testLlm(provider)
      setResult({ ok: true, text: `连接成功，模型回复：${reply}` })
    } catch (e) {
      setResult({ ok: false, text: (e as Error).message })
    } finally {
      setTesting(false)
    }
  }

  return (
    <div className="space-y-3 rounded-md border p-4">
      <div className="grid gap-3 md:grid-cols-[1fr_200px_auto]">
        <Field label="名称">
          <Input value={provider.name} onChange={(e) => set('name', e.target.value)} />
        </Field>
        <Field label="协议">
          <Select value={provider.protocol} onChange={(e) => set('protocol', e.target.value as LlmProvider['protocol'])}>
            <option value="openai">OpenAI 兼容</option>
            <option value="anthropic">Anthropic 兼容</option>
            <option value="gemini">Google Gemini 原生（v1beta）</option>
          </Select>
        </Field>
        <div className="flex items-end">
          <Button variant="ghost" size="icon" onClick={onRemove} title="删除">
            <Trash2 className="h-4 w-4" />
          </Button>
        </div>
      </div>
      <div className="grid gap-3 md:grid-cols-2">
        <Field
          label="Base URL"
          hint={
            { openai: '例如 https://api.openai.com/v1', anthropic: '例如 https://api.anthropic.com', gemini: '例如 https://generativelanguage.googleapis.com（不用带 /v1beta）' }[provider.protocol]
          }
        >
          <Input value={provider.base_url} onChange={(e) => set('base_url', e.target.value)} />
        </Field>
        <Field label="默认模型">
          <Input value={provider.model} onChange={(e) => set('model', e.target.value)} />
        </Field>
      </div>
      <Field label="API Key">
        <SecretInput value={provider.api_key} onChange={(v) => set('api_key', v)} />
      </Field>
      <div className="flex flex-wrap items-center gap-4">
        <Checkbox checked={provider.use_proxy} onChange={(v) => set('use_proxy', v)} label="通过代理访问" />
        <Button size="sm" variant="outline" onClick={test} disabled={testing}>
          {testing ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : <Zap className="mr-2 h-4 w-4" />}
          测试连接
        </Button>
        {result && (
          <span className={result.ok ? 'text-sm text-emerald-700' : 'text-sm text-red-700'}>{result.text}</span>
        )}
      </div>
    </div>
  )
}

export function newProvider(): LlmProvider {
  return {
    id: crypto.randomUUID(),
    name: '新供应商',
    protocol: 'openai',
    base_url: '',
    api_key: '',
    model: '',
    use_proxy: true,
  }
}

export function ProviderList({
  providers,
  onChange,
}: {
  providers: LlmProvider[]
  onChange: (providers: LlmProvider[]) => void
}) {
  return (
    <div className="space-y-3">
      {providers.length === 0 && <p className="text-sm text-muted-foreground">还没有供应商，先添加一个。</p>}
      {providers.map((provider, index) => (
        <ProviderRow
          key={provider.id}
          provider={provider}
          onChange={(p) => onChange(providers.map((x, i) => (i === index ? p : x)))}
          onRemove={() => onChange(providers.filter((_, i) => i !== index))}
        />
      ))}
      <Button variant="outline" size="sm" onClick={() => onChange([...providers, newProvider()])}>
        <Plus className="mr-2 h-4 w-4" />
        添加供应商
      </Button>
    </div>
  )
}

export { SecretInput }
