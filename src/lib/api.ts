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

/** 直播间截下来的采访：来源、录音起点、采访起止（live.json）和截取范围内的直播转写 */
export interface LiveStatus {
  monitor_id?: number
  stream?: { url: string; label?: string; provider?: string }
  recording?: { started_at: number; active: boolean }
  interview?: { start: number; end: number; clip_start?: number; source: string; captured_at: number }
  detection?: { interviewees?: string[]; team?: string | null }
  transcript: LiveLine[]
}

export interface MonitorStreamConfig {
  url: string
  label?: string
  /** 走不走代理；不填按域名判断（国内平台直连） */
  proxy?: boolean | null
  /** 转写语言，'' 为自动识别 */
  language?: string | null
}

export interface ShotRun {
  start: number
  end: number
  open: boolean
}

export interface MonitorStreamStatus {
  url: string
  label: string
  proxy: boolean
  platform: string
  /** offline 未开播 / recording 录制中 / replay 拉到回放 / unknown 探测失败 / stopped 没在监视 */
  status: 'starting' | 'offline' | 'recording' | 'replay' | 'unknown' | 'stopped'
  status_at: number | null
  title: string
  error: string
  recording: boolean
  connected: boolean
  origin: number | null
  chunk_count: number
  shot_runs: ShotRun[]
  detection: {
    active?: { start: number; interviewees: string[]; team: string | null; pending_end: number | null } | null
    trigger_at?: number | null
    floor?: number
    checked_at?: number
    failures?: number
  }
  transcript?: LiveLine[]
}

export interface Monitor {
  id: number
  title: string
  event_id: string | null
  event_name: string | null
  streams: MonitorStreamConfig[]
  enabled: boolean
  windows: { start: number; end: number; match_ids?: number[] }[]
  follow_schedule: boolean
  created_at: number
  updated_at: number
  on: boolean
  stream_status: MonitorStreamStatus[]
  events: { id: number; ts: number; level: 'info' | 'warning' | 'error'; message: string }[]
  tasks: { id: number; title: string; state: string; created_at: number }[]
}

export interface MonitorInput {
  title?: string
  event_id?: string | null
  event_name?: string | null
  streams?: MonitorStreamConfig[]
  enabled?: boolean
}

export interface HltvEvent {
  id: number | null
  name: string
  matches: number
  stars: number
  first_ms: number | null
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

export interface Identified {
  interviewees: string[]
  team: string | null
  opponent: string | null
  about_match: 'just_played' | 'between_maps' | 'upcoming' | 'general' | 'unknown'
  result: 'won' | 'lost' | null
  note?: string
}

export interface TaskContext {
  /** 直播间监视截下来的采访才有，背景信息里的比赛可能只有赛事名 */
  match?: Partial<MatchContext> & { event?: string; source?: string }
  monitor?: { id: number; event_name?: string | null; event_id?: string | null }
  identified?: Identified
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
  /** 预约了这场比赛的直播间和监视时间窗 */
  monitor_id: number | null
  monitor_title: string | null
  window: { start: number; end: number } | null
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
    chunk_seconds: number
    frame_interval: number
    detect_interval: number
    max_interview_minutes: number
    padding_seconds: number
  }
  monitor: {
    pre_hours: number
    post_hours: number
    map_hours: number
    offline_poll_seconds: number
    live_probe_seconds: number
    retain_hours: number
    trigger_phrases: string[]
  }
  package: {
    title_examples: string[]
    summary_examples: string[]
    hosts: string[]
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

  listMonitors: () => request<Monitor[]>('GET', '/api/monitors'),
  getMonitor: (id: number, tail = 300) => request<Monitor>('GET', `/api/monitors/${id}?tail=${tail}`),
  createMonitor: (body: MonitorInput) => request<Monitor>('POST', '/api/monitors', body),
  updateMonitor: (id: number, body: MonitorInput) => request<Monitor>('PUT', `/api/monitors/${id}`, body),
  deleteMonitor: (id: number) => request<{ ok: boolean }>('DELETE', `/api/monitors/${id}`),
  monitorEvents: () => request<{ events: HltvEvent[]; error: string | null }>('GET', '/api/monitors/events'),
  monitorAudioUrl: (id: number, index: number, start: number, end: number) =>
    `/api/monitors/${id}/streams/${index}/audio?start=${start.toFixed(1)}&end=${end.toFixed(1)}`,
  monitorCapture: (id: number, index: number, start: number, end: number, interviewees: string[]) =>
    request<Task>('POST', `/api/monitors/${id}/streams/${index}/capture`, { start, end, interviewees }),

  listMatches: (refresh = false) =>
    request<{ fetched_at: number | null; matches: HltvMatch[] }>('GET', `/api/matches?refresh=${refresh}`),
  reserveMatch: (id: number) =>
    request<{ title: string; monitor: Monitor; window: { start: number; end: number } | null; needs_stream: boolean }>(
      'POST', `/api/matches/${id}/reserve`, {}),
  unreserveMatch: (id: number) => request<{ title: string }>('DELETE', `/api/matches/${id}/reserve`),

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
