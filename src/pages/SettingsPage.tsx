import { useState, useEffect } from 'react'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { useAppStore } from '@/stores/appStore'
import { Save, Eye, EyeOff, Loader2 } from 'lucide-react'

interface BailianModel {
  id: string
  name: string
  description?: string
}

export function SettingsPage() {
  const { settings, setSettings } = useAppStore()
  const [localSettings, setLocalSettings] = useState(settings)
  const [showApiKey, setShowApiKey] = useState(false)
  const [saved, setSaved] = useState(false)
  const [bailianModels, setBailianModels] = useState<BailianModel[]>([])
  const [loadingModels, setLoadingModels] = useState(false)

  useEffect(() => {
    // 从electron-store加载设置
    const loadSettings = async () => {
      const savedSettings = await window.electronAPI.getStore('settings')
      if (savedSettings) {
        setLocalSettings(savedSettings)
        setSettings(savedSettings)
      }
    }
    loadSettings()
  }, [setSettings])

  // 当提供商切换到百炼时，获取模型列表
  useEffect(() => {
    if (localSettings.llm.provider === 'bailian' && localSettings.llm.apiKey) {
      fetchBailianModels()
    }
  }, [localSettings.llm.provider, localSettings.llm.apiKey])

  const fetchBailianModels = async () => {
    if (!localSettings.llm.apiKey) return
    setLoadingModels(true)
    try {
      const result = await window.electronAPI.getBailianModels(localSettings.llm.apiKey)
      if (result && result.models) {
        setBailianModels(result.models)
      }
    } catch (error) {
      console.error('获取百炼模型列表失败:', error)
    } finally {
      setLoadingModels(false)
    }
  }

  const handleSave = async () => {
    await window.electronAPI.setStore('settings', localSettings)
    setSettings(localSettings)
    setSaved(true)
    setTimeout(() => setSaved(false), 2000)
  }

  const updateLLM = (key: string, value: string) => {
    setLocalSettings((prev) => ({
      ...prev,
      llm: { ...prev.llm, [key]: value },
    }))
  }

  const updateWhisper = (key: string, value: string) => {
    setLocalSettings((prev) => ({
      ...prev,
      whisper: { ...prev.whisper, [key]: value },
    }))
  }

  const updateDiarization = (key: string, value: string) => {
    setLocalSettings((prev) => ({
      ...prev,
      diarization: { ...prev.diarization, [key]: value },
    }))
  }

  const updateOutput = (key: string, value: string) => {
    setLocalSettings((prev) => ({
      ...prev,
      output: { ...prev.output, [key]: value },
    }))
  }

  return (
    <div className="max-w-2xl mx-auto space-y-8">
      <div>
        <h1 className="text-2xl font-bold">设置</h1>
        <p className="text-muted-foreground">配置应用程序选项</p>
      </div>

      {/* LLM设置 */}
      <div className="border rounded-lg p-6 space-y-4">
        <h2 className="text-lg font-semibold">LLM配置</h2>

        <div className="space-y-2">
          <label className="text-sm font-medium">提供商</label>
          <select
            value={localSettings.llm.provider}
            onChange={(e) => updateLLM('provider', e.target.value)}
            className="w-full border rounded px-3 py-2"
          >
            <option value="bailian">阿里百炼 (推荐)</option>
            <option value="openai">OpenAI</option>
            <option value="gemini">Google Gemini (官方)</option>
            <option value="anthropic">Anthropic (Claude)</option>
            <option value="relay_openai">OpenAI格式中转站</option>
            <option value="relay_anthropic">Anthropic格式中转站</option>
          </select>
        </div>

        <div className="space-y-2">
          <label className="text-sm font-medium">API Key</label>
          <div className="relative">
            <Input
              type={showApiKey ? 'text' : 'password'}
              value={localSettings.llm.apiKey}
              onChange={(e) => updateLLM('apiKey', e.target.value)}
              placeholder={getApiKeyPlaceholder(localSettings.llm.provider)}
            />
            <button
              type="button"
              onClick={() => setShowApiKey(!showApiKey)}
              className="absolute right-2 top-1/2 -translate-y-1/2 text-muted-foreground"
            >
              {showApiKey ? <EyeOff className="h-4 w-4" /> : <Eye className="h-4 w-4" />}
            </button>
          </div>
        </div>

        <div className="space-y-2">
          <label className="text-sm font-medium">模型</label>
          {localSettings.llm.provider === 'bailian' ? (
            <div className="flex gap-2">
              <select
                value={localSettings.llm.model}
                onChange={(e) => updateLLM('model', e.target.value)}
                className="flex-1 border rounded px-3 py-2"
                disabled={loadingModels}
              >
                {bailianModels.length > 0 ? (
                  bailianModels.map((model) => (
                    <option key={model.id} value={model.id}>
                      {model.name} - {model.description || model.id}
                    </option>
                  ))
                ) : (
                  <>
                    <option value="qwen-turbo">通义千问-Turbo - 快速响应</option>
                    <option value="qwen-plus">通义千问-Plus - 平衡性能</option>
                    <option value="qwen-max">通义千问-Max - 最强能力</option>
                    <option value="qwen-max-longcontext">通义千问-Max长文本 - 超长上下文</option>
                    <option value="qwen-long">通义千问-Long - 长文本专家</option>
                    <option value="qwen2.5-72b-instruct">Qwen2.5-72B-Instruct - 开源最强</option>
                    <option value="qwen2.5-32b-instruct">Qwen2.5-32B-Instruct - 中等开源</option>
                    <option value="qwen2.5-14b-instruct">Qwen2.5-14B-Instruct - 轻量开源</option>
                    <option value="qwen2.5-7b-instruct">Qwen2.5-7B-Instruct - 最小开源</option>
                    <option value="deepseek-r1">DeepSeek-R1 - 深度求索推理</option>
                    <option value="deepseek-v3">DeepSeek-V3 - 深度求索通用</option>
                    <option value="yi-lightning">零一万物-Lightning - 快速版</option>
                    <option value="yi-large">零一万物-Large - 大模型</option>
                  </>
                )}
              </select>
              <Button
                variant="outline"
                onClick={fetchBailianModels}
                disabled={loadingModels || !localSettings.llm.apiKey}
                title="刷新模型列表"
              >
                {loadingModels ? <Loader2 className="h-4 w-4 animate-spin" /> : '刷新'}
              </Button>
            </div>
          ) : (
            <Input
              value={localSettings.llm.model}
              onChange={(e) => updateLLM('model', e.target.value)}
              placeholder={getModelPlaceholder(localSettings.llm.provider)}
            />
          )}
        </div>

        {(localSettings.llm.provider === 'local' || localSettings.llm.provider === 'relay_openai' || localSettings.llm.provider === 'relay_anthropic') && (
          <div className="space-y-2">
            <label className="text-sm font-medium">
              {localSettings.llm.provider === 'local' ? 'Ollama地址' : '自定义API地址'}
            </label>
            <Input
              value={localSettings.llm.baseUrl || ''}
              onChange={(e) => updateLLM('baseUrl', e.target.value)}
              placeholder={
                localSettings.llm.provider === 'local'
                  ? 'http://localhost:11434'
                  : localSettings.llm.provider === 'relay_anthropic'
                  ? 'https://api.anthropic.com'
                  : 'https://api.openai.com/v1'
              }
            />
          </div>
        )}
      </div>

      {/* Whisper设置 */}
      <div className="border rounded-lg p-6 space-y-4">
        <h2 className="text-lg font-semibold">Whisper 语音识别</h2>

        <div className="space-y-2">
          <label className="text-sm font-medium">模型目录</label>
          <p className="text-xs text-muted-foreground">下载 faster-whisper 模型后解压到此目录</p>
          <div className="flex gap-2">
            <Input
              value={localSettings.whisper.modelPath}
              onChange={(e) => updateWhisper('modelPath', e.target.value)}
              placeholder="例如: C:\CS-Translator\models\whisper"
              className="flex-1"
            />
            <Button
              variant="outline"
              onClick={async () => {
                const dir = await window.electronAPI.selectDirectory()
                if (dir) updateWhisper('modelPath', dir)
              }}
            >
              浏览
            </Button>
          </div>
        </div>

        <div className="space-y-2">
          <label className="text-sm font-medium">运行设备</label>
          <select
            value={localSettings.whisper.device}
            onChange={(e) => updateWhisper('device', e.target.value)}
            className="w-full border rounded px-3 py-2"
          >
            <option value="cuda">CUDA (NVIDIA GPU 加速)</option>
            <option value="cpu">CPU (较慢)</option>
          </select>
        </div>
      </div>

      {/* 说话人分离设置 */}
      <div className="border rounded-lg p-6 space-y-4">
        <h2 className="text-lg font-semibold">说话人分离 (Diarization)</h2>

        <div className="space-y-2">
          <label className="text-sm font-medium">模型目录</label>
          <p className="text-xs text-muted-foreground">下载 pyannote 模型后解压到此目录，留空则自动从 HuggingFace 下载</p>
          <div className="flex gap-2">
            <Input
              value={localSettings.diarization.modelPath}
              onChange={(e) => updateDiarization('modelPath', e.target.value)}
              placeholder="例如: C:\CS-Translator\models\diarization"
              className="flex-1"
            />
            <Button
              variant="outline"
              onClick={async () => {
                const dir = await window.electronAPI.selectDirectory()
                if (dir) updateDiarization('modelPath', dir)
              }}
            >
              浏览
            </Button>
          </div>
        </div>

        <div className="space-y-2">
          <label className="text-sm font-medium">HuggingFace Token</label>
          <p className="text-xs text-muted-foreground">
            仅在使用远程模型时需要，在{' '}
            <a href="https://huggingface.co/settings/tokens" target="_blank" rel="noreferrer" className="underline">
              huggingface.co/settings/tokens
            </a>{' '}
            创建
          </p>
          <Input
            type={showApiKey ? 'text' : 'password'}
            value={localSettings.diarization.hfToken}
            onChange={(e) => updateDiarization('hfToken', e.target.value)}
            placeholder="hf_xxx"
          />
        </div>
      </div>

      {/* 输出设置 */}
      <div className="border rounded-lg p-6 space-y-4">
        <h2 className="text-lg font-semibold">输出配置</h2>

        <div className="space-y-2">
          <label className="text-sm font-medium">默认输出目录</label>
          <div className="flex gap-2">
            <Input
              value={localSettings.output.defaultDir}
              onChange={(e) => updateOutput('defaultDir', e.target.value)}
              placeholder="选择目录..."
              className="flex-1"
            />
            <Button
              variant="outline"
              onClick={async () => {
                const dir = await window.electronAPI.selectDirectory()
                if (dir) updateOutput('defaultDir', dir)
              }}
            >
              浏览
            </Button>
          </div>
        </div>

        <div className="space-y-2">
          <label className="text-sm font-medium">导出格式</label>
          <select
            value={localSettings.output.format}
            onChange={(e) => updateOutput('format', e.target.value)}
            className="w-full border rounded px-3 py-2"
          >
            <option value="markdown">Markdown (.md)</option>
            <option value="html">HTML (.html)</option>
            <option value="txt">纯文本 (.txt)</option>
          </select>
        </div>
      </div>

      <div className="flex justify-end">
        <Button onClick={handleSave}>
          <Save className="h-4 w-4 mr-2" />
          {saved ? '已保存' : '保存设置'}
        </Button>
      </div>
    </div>
  )
}

function getModelPlaceholder(provider: string): string {
  switch (provider) {
    case 'bailian':
      return 'qwen-turbo'
    case 'openai':
      return 'gpt-4-turbo-preview'
    case 'gemini':
      return 'gemini-3.1-flash-lite'
    case 'anthropic':
      return 'claude-3-opus-20240229'
    case 'openrouter':
      return 'anthropic/claude-3-opus'
    case 'relay_openai':
      return 'gpt-4o'
    case 'relay_anthropic':
      return 'claude-sonnet-4-6'
    case 'local':
      return 'llama2'
    default:
      return ''
  }
}

function getApiKeyPlaceholder(provider: string): string {
  if (provider === 'gemini') {
    return 'AIza...'
  }
  return 'sk-...'
}
