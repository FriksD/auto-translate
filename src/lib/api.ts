// 后端 API 客户端与类型定义

export interface StageMeta {
  key: string
  label: string
}

export interface TaskProgress {
  percent: number | null
  message: string
  updated_at: number
}

export interface TeamContext {
  name: string
  players?: string[]
  player_details?: { name: string; real_name?: string; nationality?: string }[]
  stats?: { coach?: string | null; world_ranking?: string | null }
  coach?: string
  ranking?: string
  recent_matches?: { team1: string; team2: string; score: string; event: string }[]
  error?: string
}

export interface TaskContext {
  teams?: TeamContext[]
  extra_names?: string[]
  background?: string
  match_summary?: string
}

export interface Task {
  id: number
  title: string
  state: string
  stage: string | null
  source_type: 'url' | 'file'
  source_url: string | null
  source_file: string | null
  clip_start: number | null
  clip_end: number | null
  team1: string | null
  team2: string | null
  notes: string | null
  context: TaskContext
  error: string | null
  created_at: number
  updated_at: number
  progress?: TaskProgress | null
}

export interface TaskEvent {
  id: number
  ts: number
  level: 'info' | 'warning' | 'error'
  stage: string | null
  message: string
}

export interface NamesReport {
  replaced: { from: string; to: string; count: number }[]
  suspects: { token: string; candidates: string[] }[]
  ambiguous: { token: string; candidates: string[] }[]
  unknown: string[]
}

export interface TranscriptSegment {
  start: number
  end: number
  text: string
  speaker?: string
}

export interface TaskDetail extends Task {
  events: TaskEvent[]
  files: { name: string; size: number; mtime: number }[]
  texts: Record<string, string | null>
  names_report: NamesReport | null
  transcript: { segments: TranscriptSegment[]; language: string } | null
  docx: string | null
}

export interface LlmProvider {
  id: string
  name: string
  protocol: 'openai' | 'anthropic'
  base_url: string
  api_key: string
  model: string
  use_proxy: boolean
}

export interface LlmStep {
  provider_id: string
  model: string
}

export interface Settings {
  proxy: string
  ffmpeg_path: string
  whisper: { model: string; device: string; compute_type: string; language: string; beam_size: number }
  diarization: { enabled: boolean; model_path: string; hf_token: string }
  llm: {
    providers: LlmProvider[]
    clean: LlmStep
    write: LlmStep
    temperature: number
    extra_instructions: string
  }
  feishu: {
    enabled: boolean
    app_id: string
    app_secret: string
    receive_id_type: 'chat_id' | 'open_id'
    receive_id: string
  }
}

export interface GlossaryEntry {
  id?: number
  kind: 'alias' | 'term'
  term: string
  target: string
  note?: string | null
}

async function request<T>(method: string, url: string, body?: unknown): Promise<T> {
  const init: RequestInit = { method }
  if (body instanceof FormData) {
    init.body = body
  } else if (body !== undefined) {
    init.headers = { 'Content-Type': 'application/json' }
    init.body = JSON.stringify(body)
  }
  const response = await fetch(url, init)
  if (!response.ok) {
    let message = `${response.status} ${response.statusText}`
    try {
      const data = await response.json()
      if (data?.detail) message = typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail)
    } catch {
      // 非 JSON 错误体，保留状态码
    }
    throw new Error(message)
  }
  return response.json() as Promise<T>
}

export const api = {
  meta: () => request<{ stages: StageMeta[] }>('GET', '/api/meta'),

  listTasks: () => request<Task[]>('GET', '/api/tasks'),
  getTask: (id: number) => request<TaskDetail>('GET', `/api/tasks/${id}`),
  createTask: (body: Partial<Task> & { start?: boolean }) => request<Task>('POST', '/api/tasks', body),
  updateTask: (id: number, body: Partial<Task>) => request<Task>('PATCH', `/api/tasks/${id}`, body),
  deleteTask: (id: number) => request<{ ok: boolean }>('DELETE', `/api/tasks/${id}`),
  uploadSource: (id: number, file: File) => {
    const form = new FormData()
    form.append('file', file)
    return request<Task>('POST', `/api/tasks/${id}/upload`, form)
  },
  runTask: (id: number, fromStage: string, toStage?: string) =>
    request<Task>('POST', `/api/tasks/${id}/run`, { from_stage: fromStage, to_stage: toStage ?? null }),
  cancelTask: (id: number) => request<Task>('POST', `/api/tasks/${id}/cancel`),
  markDone: (id: number) => request<Task>('POST', `/api/tasks/${id}/done`),
  saveText: (id: number, name: string, text: string) =>
    request<{ ok: boolean }>('PUT', `/api/tasks/${id}/texts/${name}`, { text }),
  fileUrl: (id: number, name: string) => `/api/tasks/${id}/files/${encodeURIComponent(name)}`,

  getSettings: () => request<Settings>('GET', '/api/settings'),
  saveSettings: (settings: Settings) => request<Settings>('PUT', '/api/settings', settings),
  testLlm: (provider: LlmProvider) => request<{ reply: string }>('POST', '/api/llm/test', { provider }),
  testFeishu: (feishu: Settings['feishu']) => request<{ ok: boolean }>('POST', '/api/feishu/test', feishu),

  listGlossary: () => request<GlossaryEntry[]>('GET', '/api/glossary'),
  saveGlossary: (entry: GlossaryEntry) => request<GlossaryEntry>('POST', '/api/glossary', entry),
  deleteGlossary: (id: number) => request<{ ok: boolean }>('DELETE', `/api/glossary/${id}`),
}

export const STATE_LABELS: Record<string, string> = {
  draft: '未开始',
  queued: '排队中',
  processing: '处理中',
  review: '待精校',
  done: '已完成',
  failed: '失败',
  cancelled: '已取消',
  reserved: '已预约',
  watching: '监视中',
  recording: '录制中',
  interview_detecting: '采访监听中',
  no_interview: '无采访',
}

export const STATE_COLORS: Record<string, string> = {
  draft: 'bg-slate-100 text-slate-700',
  queued: 'bg-blue-50 text-blue-700',
  processing: 'bg-blue-100 text-blue-800',
  review: 'bg-amber-100 text-amber-800',
  done: 'bg-emerald-100 text-emerald-800',
  failed: 'bg-red-100 text-red-700',
  cancelled: 'bg-slate-100 text-slate-500',
}

export function isRunning(state: string) {
  return state === 'queued' || state === 'processing'
}

export function formatTime(ts: number) {
  return new Date(ts * 1000).toLocaleString('zh-CN', { hour12: false })
}

export function formatSeconds(seconds: number | null | undefined) {
  if (seconds === null || seconds === undefined) return ''
  const total = Math.max(0, Math.floor(seconds))
  const m = Math.floor(total / 60)
  const s = total % 60
  return `${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`
}

/** 解析 "mm:ss"、"hh:mm:ss" 或纯秒数，空字符串返回 null */
export function parseSeconds(text: string): number | null {
  const value = text.trim()
  if (!value) return null
  const parts = value.split(':').map(Number)
  if (parts.some((p) => Number.isNaN(p))) throw new Error(`无法解析时间：${text}`)
  return parts.reduce((acc, p) => acc * 60 + p, 0)
}
