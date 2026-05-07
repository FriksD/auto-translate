interface TranscriptSegment {
  start: number
  end: number
  text: string
  speaker?: string
}

interface Transcript {
  segments: TranscriptSegment[]
}

interface TranscriptionProgress {
  status: 'idle' | 'running' | 'completed' | 'error'
  stage: string
  progress: number
  message: string
  audio_path?: string | null
  processed_duration: number
  total_duration?: number | null
  segment_count: number
  language?: string | null
  language_probability?: number | null
  error?: string | null
  started_at?: number | null
  updated_at?: number | null
}

interface DiarizationProgress {
  status: 'idle' | 'running' | 'completed' | 'error'
  stage: string
  progress: number
  message: string
  audio_path?: string | null
  processed_duration: number
  total_duration?: number | null
  chunk_count: number
  total_chunks: number
  error?: string | null
  started_at?: number | null
  updated_at?: number | null
}

interface SpeakerSegment {
  speaker: string
  start: number
  end: number
}

interface DiarizationResult {
  segments: SpeakerSegment[]
}

interface SpeakerMapping {
  [key: string]: string
}

interface HLTVContext {
  teams?: any[]
  players?: any[]
  customContext?: string
}

interface LLMConfig {
  provider: 'openai' | 'anthropic' | 'openrouter' | 'local' | 'bailian' | 'gemini' | 'relay_openai' | 'relay_anthropic'
  apiKey: string
  model: string
  baseUrl?: string
  promptOverride?: string
}

interface VideoInfo {
  id: string
  title: string
  description: string
  duration: number
  thumbnail: string
  author: string
}

interface HLTVInfo {
  type: 'team' | 'player'
  name: string
  data: any
  error?: string
  ranking?: string | number
  points?: string
  players?: string[]
  player_details?: any[]
  real_name?: string
  team?: string
  nationality?: string
  age?: number | null
  stats?: Record<string, any>
}

interface BailianModelsResponse {
  models: BailianModel[]
}

interface BailianModel {
  id: string
  name: string
  description?: string
}

interface ElectronAPI {
  getVideoInfo: (url: string) => Promise<VideoInfo>
  downloadAudio: (url: string, outputDir: string) => Promise<string>
  transcribe: (audioPath: string) => Promise<Transcript>
  getTranscriptionProgress: (audioPath?: string) => Promise<TranscriptionProgress>
  diarize: (audioPath: string, numSpeakers?: number) => Promise<DiarizationResult>
  getDiarizationProgress: (audioPath?: string) => Promise<DiarizationProgress>
  translate: (transcript: Transcript, speakers: SpeakerMapping, context: HLTVContext, config: LLMConfig) => Promise<string>
  previewPrompt: (transcript: Transcript, speakers: SpeakerMapping, context: HLTVContext) => Promise<string>
  getHLTVInfo: (query: string, type: 'team' | 'player') => Promise<HLTVInfo>
  getBailianModels: (apiKey: string) => Promise<BailianModelsResponse>
  getStore: (key: string) => Promise<any>
  setStore: (key: string, value: any) => Promise<boolean>
  selectDirectory: () => Promise<string | null>
  selectFile: (filters: any[]) => Promise<string | null>
}

declare global {
  interface Window {
    electronAPI: ElectronAPI
  }
}

export {}
