import { contextBridge, ipcRenderer } from 'electron'

// 暴露给渲染进程的API
contextBridge.exposeInMainWorld('electronAPI', {
  // YouTube相关
  getVideoInfo: (url: string) => ipcRenderer.invoke('get-video-info', url),
  downloadAudio: (url: string, outputDir: string) => ipcRenderer.invoke('download-audio', url, outputDir),

  // 转录相关
  transcribe: (audioPath: string) => ipcRenderer.invoke('transcribe', audioPath),
  getTranscriptionProgress: (audioPath?: string) => ipcRenderer.invoke('get-transcription-progress', audioPath),
  diarize: (audioPath: string, numSpeakers?: number) => ipcRenderer.invoke('diarize', audioPath, numSpeakers),
  getDiarizationProgress: (audioPath?: string) => ipcRenderer.invoke('get-diarization-progress', audioPath),

  // 翻译相关
  translate: (transcript: any, speakers: any, context: any, config: any) =>
    ipcRenderer.invoke('translate', transcript, speakers, context, config),
  previewPrompt: (transcript: any, speakers: any, context: any) =>
    ipcRenderer.invoke('preview-prompt', transcript, speakers, context),

  // HLTV相关
  getHLTVInfo: (query: string, type: 'team' | 'player') =>
    ipcRenderer.invoke('get-hltv-info', query, type),

  // 百炼模型列表
  getBailianModels: (apiKey: string) => ipcRenderer.invoke('get-bailian-models', apiKey),

  // 存储
  getStore: (key: string) => ipcRenderer.invoke('get-store', key),
  setStore: (key: string, value: any) => ipcRenderer.invoke('set-store', key, value),

  // 文件对话框
  selectDirectory: () => ipcRenderer.invoke('select-directory'),
  selectFile: (filters: any[]) => ipcRenderer.invoke('select-file', filters),
})

// 类型定义
export interface ElectronAPI {
  getVideoInfo: (url: string) => Promise<VideoInfo>
  downloadAudio: (url: string, outputDir: string) => Promise<string>
  transcribe: (audioPath: string) => Promise<Transcript>
  getTranscriptionProgress: (audioPath?: string) => Promise<TranscriptionProgress>
  diarize: (audioPath: string, numSpeakers?: number) => Promise<DiarizationResult>
  getDiarizationProgress: (audioPath?: string) => Promise<DiarizationProgress>
  translate: (transcript: Transcript, speakers: SpeakerMapping, context: HLTVContext, config: LLMConfig) => Promise<string>
  previewPrompt: (transcript: Transcript, speakers: SpeakerMapping, context: HLTVContext) => Promise<string>
  getHLTVInfo: (query: string, type: 'team' | 'player') => Promise<HLTVInfo>
  getBailianModels: (apiKey: string) => Promise<BailianModel[]>
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

interface VideoInfo {
  title: string
  description: string
  duration: number
  thumbnail: string
  author: string
}

interface Transcript {
  segments: TranscriptSegment[]
}

interface TranscriptSegment {
  start: number
  end: number
  text: string
  speaker?: string
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
  teams?: TeamInfo[]
  players?: PlayerInfo[]
  customContext?: string
}

interface LLMConfig {
  provider: 'openai' | 'anthropic' | 'openrouter' | 'local' | 'bailian' | 'gemini' | 'relay_openai' | 'relay_anthropic'
  apiKey: string
  model: string
  baseUrl?: string
  promptOverride?: string
}

interface HLTVInfo {
  type: 'team' | 'player'
  name: string
  data: any
}

interface TeamInfo {
  name: string
  ranking: number
  players: string[]
  recentResults: MatchResult[]
}

interface PlayerInfo {
  name: string
  team: string
  nationality: string
  stats: PlayerStats
}

interface PlayerStats {
  rating: number
  kdRatio: number
  maps: number
}

interface MatchResult {
  opponent: string
  result: string
  date: string
}

interface BailianModel {
  id: string
  name: string
  description?: string
}
