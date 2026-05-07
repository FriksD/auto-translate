import Store from 'electron-store'

export interface AppSettings {
  llm: {
    provider: 'openai' | 'anthropic' | 'openrouter' | 'local' | 'bailian' | 'gemini' | 'relay_openai' | 'relay_anthropic'
    apiKey: string
    model: string
    baseUrl?: string
  }
  whisper: {
    device: 'cuda' | 'cpu'
    modelPath: string
  }
  diarization: {
    modelPath: string
    hfToken: string
  }
  output: {
    defaultDir: string
    format: 'markdown' | 'html' | 'txt'
  }
  hltv: {
    enabled: boolean
    autoFetch: boolean
  }
}

const defaultSettings: AppSettings = {
  llm: {
    provider: 'bailian',
    apiKey: '',
    model: 'qwen-turbo',
  },
  whisper: {
    device: 'cuda',
    modelPath: '',
  },
  diarization: {
    modelPath: '',
    hfToken: '',
  },
  output: {
    defaultDir: '',
    format: 'markdown',
  },
  hltv: {
    enabled: true,
    autoFetch: false,
  },
}

export const store = new Store<AppSettings>({
  defaults: defaultSettings,
})

export function getSettings(): AppSettings {
  return {
    llm: store.get('llm'),
    whisper: store.get('whisper'),
    output: store.get('output'),
    hltv: store.get('hltv'),
  }
}

export function updateSettings(settings: Partial<AppSettings>): void {
  if (settings.llm) store.set('llm', settings.llm)
  if (settings.whisper) store.set('whisper', settings.whisper)
  if (settings.diarization) store.set('diarization', settings.diarization)
  if (settings.output) store.set('output', settings.output)
  if (settings.hltv) store.set('hltv', settings.hltv)
}
