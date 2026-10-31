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

export interface PastMatch {
  opponent: string
  score: string
  result: 'won' | 'lost' | 'draw'
  format: string
  when: string
  url: string
}

export interface MapStat {
  map: string
  win_pct: string
  pickban: string
  pick_pct: number | null
  ban_pct: number | null
  wins: number | null
  losses: number | null
}

export interface TeamContext {
  id?: number | null
  name: string
  players?: string[]
  player_details?: { name: string; real_name?: string; nationality?: string; standin_for?: string }[]
  stats?: { coach?: string | null }
  coach?: string
  coach_full?: string
  hltv_rank?: number | null
  vrs_rank?: number | null
  past_matches?: PastMatch[]
  map_stats?: MapStat[]
  news?: { title: string; url: string; unix_ms: number | null }[]
  error?: string
}

export interface Stream {
  provider: string
  name: string
  country: string
  viewers: number | null
  url: string
  source?: 'hltv' | 'bo3' | 'memory'
}

export interface MapResult {
  name: string
  team1_score: number | null
  team2_score: number | null
  picked_by: string | null
  status?: 'finished' | 'live' | 'upcoming'
  winner?: 1 | 2 | null
}

export interface LiveLine {
  start: number
  end: number
  text: string
}

export interface LiveStatus {
  snapshot?: {
    status: 'upcoming' | 'live' | 'over'
    best_of: number
    map_wins: number[]
    live: { map: string; round: number | null; team1: number; team2: number; source: string } | null
    deciding: boolean
    overtime: boolean
    interval: number
    reason: string
    should_record: boolean
    summary: string
    maps: MapResult[]
    checked_at: number
    next_check_at: number
    source?: 'bo3' | 'hltv'
    source_url?: string
    round_source?: 'hltv' | null
    failures?: { source: string; error: string }[]
  }
  source_ids?: { bo3?: { slug: string; swapped: boolean } }
  source_down_since?: number | null
  streams?: Stream[]
  stream?: Partial<Stream> & { url: string }
  recording?: { started_at: number; active: boolean; stopped_at?: number }
  match_over_at?: number
  /** 画面识别出的采访镜头段（相对录制开始的秒数） */
  shot_runs?: { start: number; end: number; open: boolean }[]
  frame_count?: number
  shot_count?: number
  detection?: {
    started?: boolean
    start?: number
    interviewee?: string | null
    checked_at?: number
    pending_end?: number | null
    failures?: number
    last?: { started: boolean; ended: boolean; start: string | null; end: string | null; confidence?: number; note?: string }
  }
  interview?: { start: number; end: number; source: string; captured_at: number }
  recording_active: boolean
  connected: boolean
  chunk_count: number
  recorded_seconds: number
  transcript: LiveLine[]
}

export interface MatchContext {
  id: number
  url: string
  event: string
  format: string
  unix_ms: number | null
  status: 'upcoming' | 'live' | 'over'
  score: (number | null)[]
  veto: string[]
  maps: MapResult[]
  head_to_head: { team1_wins: number; overtimes: number; team2_wins: number } | null
  streams: Stream[]
}

export interface TaskContext {
  match?: MatchContext
  teams?: TeamContext[]
  extra_names?: string[]
  background?: string
}

export interface HltvMatch {
  id: number
  url: string
  unix_ms: number | null
  live: boolean
  lan: boolean
  stars: number
  team1: { id: number | null; name: string }
  team2: { id: number | null; name: string }
  event: { id: number | null; name: string }
  format: string
  stage: string
  task_id: number | null
}

export interface Task {
  id: number
  title: string
  state: string
  stage: string | null
  source_type: 'url' | 'file' | 'live'
  source_url: string | null
  source_file: string | null
  clip_start: number | null
  clip_end: number | null
  team1: string | null
  team2: string | null
  match_hltv_id: string | null
  match_time: number | null
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
  replaced: { from: string; to: string; count: number; method: 'exact' | 'alias' | 'phonetic' | 'skeleton' | 'llm' }[]
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
  protocol: 'openai' | 'anthropic' | 'gemini'
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
  hltv: { headless: boolean }
  whisper: { model: string; device: string; compute_type: string; language: string; beam_size: number }
  diarization: { enabled: boolean; model_path: string; hf_token: string }
  llm: {
    providers: LlmProvider[]
    clean: LlmStep
    write: LlmStep
    detect: LlmStep
    temperature: number
    extra_instructions: string
  }
  live: {
    sources: string[]
    pre_match_minutes: number
    intervals: { idle: number; mid: number; late: number; recording: number }
    mid_at: number
    late_at: number
    record_at: number
    no_score_record_minutes: number
    chunk_seconds: number
    frame_interval: number
    detect_interval: number
    alert_no_interview_minutes: number
    max_post_match_minutes: number
    max_interview_minutes: number
    padding_seconds: number
    draft_deadline_minutes: number
  }
  feishu: {
    enabled: boolean
    app_id: string
    app_secret: string
    receive_id_type: 'chat_id' | 'open_id'
    receive_id: string
    public_url: string
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
  createTask: (body: Partial<Task> & { start?: boolean; match_url?: string | null }) =>
    request<Task>('POST', '/api/tasks', body),
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

  liveStatus: (id: number) => request<LiveStatus>('GET', `/api/tasks/${id}/live`),
  liveCheck: (id: number) => request<{ ok: boolean }>('POST', `/api/tasks/${id}/live/check`),
  liveRecord: (id: number, stream?: { url: string; name?: string; provider?: string }) =>
    request<LiveStatus>('POST', `/api/tasks/${id}/live/record`, stream ?? {}),
  liveCapture: (id: number, start: number, end: number) =>
    request<Task>('POST', `/api/tasks/${id}/live/capture`, { start, end }),
  liveNoInterview: (id: number) => request<Task>('POST', `/api/tasks/${id}/live/no-interview`),
  liveMatchOver: (id: number) => request<Task>('POST', `/api/tasks/${id}/live/match-over`),
  liveSetSource: (id: number, bo3: string) => request<{ ok: boolean }>('PUT', `/api/tasks/${id}/live/source`, { bo3 }),
  liveAudioUrl: (id: number, start: number, end: number) =>
    `/api/tasks/${id}/live/audio?start=${start.toFixed(1)}&end=${end.toFixed(1)}`,

  listMatches: (refresh = false) =>
    request<{ fetched_at: number | null; matches: HltvMatch[] }>('GET', `/api/matches?refresh=${refresh}`),
  reserveMatch: (id: number) => request<Task>('POST', `/api/matches/${id}/reserve`, {}),

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
  reserved: 'bg-violet-100 text-violet-800',
  watching: 'bg-violet-100 text-violet-800',
  recording: 'bg-rose-100 text-rose-700',
  interview_detecting: 'bg-rose-100 text-rose-700',
  no_interview: 'bg-slate-100 text-slate-600',
  done: 'bg-emerald-100 text-emerald-800',
  failed: 'bg-red-100 text-red-700',
  cancelled: 'bg-slate-100 text-slate-500',
}

export function isRunning(state: string) {
  return state === 'queued' || state === 'processing'
}

/** 直播环节（预约 → 监视 → 录制 → 采访监听）中的状态 */
export const LIVE_STATES = ['reserved', 'watching', 'recording', 'interview_detecting', 'no_interview']

export function isLive(state: string) {
  return LIVE_STATES.includes(state)
}

export function formatTime(ts: number) {
  return new Date(ts * 1000).toLocaleString('zh-CN', { hour12: false })
}

export function formatMatchTime(unixMs: number | null | undefined) {
  if (!unixMs) return ''
  return new Date(unixMs).toLocaleString('zh-CN', {
    hour12: false,
    month: 'numeric',
    day: 'numeric',
    weekday: 'short',
    hour: '2-digit',
    minute: '2-digit',
  })
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
