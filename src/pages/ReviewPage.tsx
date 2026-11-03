// 在线精校页：手机上在飞书里打开，听采访音频、对照英文改中文稿，任何一人都可以定稿
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { useParams } from 'react-router-dom'
import { AlertTriangle, Check, CheckCircle2, ChevronDown, Copy, Loader2, Pause, Play, RefreshCw, RotateCcw, RotateCw, Trash2 } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { cn } from '@/lib/utils'
import { formatSeconds, STATE_COLORS, STATE_LABELS } from '@/lib/api'

interface ReviewParagraph {
  zh: string
  en: string
  start: number | null
  edited: { by: string; at: number } | null
}

interface ReviewData {
  task: { id: number; title: string; state: string; subtitle: string }
  editable: boolean
  paragraphs: ReviewParagraph[]
  has_audio: boolean
  duration: number | null
  frames: { t: number; name: string }[]
  suspects: { token: string; candidates: string[] }[]
  unknown: string[]
  finalized: { by: string; at: number } | null
  package: PackageData
  user?: { name: string }
  sent?: boolean
}

interface PackageData {
  status: 'none' | 'running' | 'done'
  titles: string[]
  title: string
  summary: string
  covers: { file: string; source: string; label: string; url: string }[]
  warnings: string[]
}

class HttpError extends Error {
  constructor(public status: number, message: string) {
    super(message)
  }
}

async function call<T>(method: string, url: string, body?: unknown): Promise<T> {
  const headers: Record<string, string> = { 'ngrok-skip-browser-warning': '1' }
  if (body !== undefined) headers['Content-Type'] = 'application/json'
  const response = await fetch(url, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) })
  if (response.status === 401) {
    window.location.href = `/auth/feishu/login?next=${encodeURIComponent(window.location.pathname)}`
    throw new HttpError(401, '正在跳转到飞书登录...')
  }
  if (!response.ok) {
    let message = `${response.status} ${response.statusText}`
    try {
      const data = await response.json()
      if (typeof data?.detail === 'string') message = data.detail
    } catch {
      // 非 JSON 错误体，保留状态码
    }
    throw new HttpError(response.status, message)
  }
  return response.json() as Promise<T>
}

function clock(ts: number) {
  const d = new Date(ts * 1000)
  return `${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')} ${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`
}

// 最后一个开始时间不晚于 t 的元素下标
function lastAtOrBefore(times: (number | null)[], t: number) {
  let found = -1
  times.forEach((value, i) => {
    if (value !== null && value <= t) found = i
  })
  return found
}

const RATES = [1, 1.25, 1.5, 0.75]
const SAVE_DELAY = 1500

export function ReviewPage() {
  const id = Number(useParams().id)
  const [data, setData] = useState<ReviewData | null>(null)
  const [error, setError] = useState('')
  const [showEnglish, setShowEnglish] = useState(true)
  const [toast, setToast] = useState('')
  const [finalizing, setFinalizing] = useState(false)

  const load = useCallback(async () => {
    try {
      setData(await call<ReviewData>('GET', `/api/review/${id}`))
      setError('')
    } catch (e) {
      setError((e as Error).message)
    }
  }, [id])

  // 别人的修改每隔一会儿拉一次；页面在后台时不拉
  useEffect(() => {
    load()
    const timer = window.setInterval(() => {
      if (document.visibilityState === 'visible') load()
    }, 8000)
    return () => window.clearInterval(timer)
  }, [load])

  useEffect(() => {
    if (!toast) return
    const timer = window.setTimeout(() => setToast(''), 3000)
    return () => window.clearTimeout(timer)
  }, [toast])

  useEffect(() => {
    if (data) document.title = `精校 · ${data.task.title}`
  }, [data])

  const save = useCallback(async (index: number, base: string, text: string) => {
    try {
      setData(await call<ReviewData>('PUT', `/api/review/${id}/paragraphs/${index}`, { base, text }))
    } catch (e) {
      if (e instanceof HttpError && e.status === 409) load()
      throw e
    }
  }, [id, load])

  const finalize = async () => {
    if (!window.confirm('定稿：用当前的中文稿重新生成 Word（摘要作为导语放在最前面），标记完成，并把 Word、标题和封面发到飞书群里？')) return
    setFinalizing(true)
    try {
      const result = await call<ReviewData>('POST', `/api/review/${id}/finalize`)
      setData((previous) => ({ ...result, user: previous?.user }))
      setToast(result.sent ? '已定稿，Word 已发到群里' : '已定稿')
    } catch (e) {
      setToast(`定稿失败：${(e as Error).message}`)
    } finally {
      setFinalizing(false)
    }
  }

  if (!data) {
    return (
      <div className="flex min-h-screen items-center justify-center p-6 text-center text-sm text-muted-foreground">
        {error || <Loader2 className="h-6 w-6 animate-spin" />}
      </div>
    )
  }

  const lastEdit = Math.max(0, ...data.paragraphs.map((p) => p.edited?.at ?? 0))
  const changedAfterFinal = data.finalized && lastEdit > data.finalized.at

  return (
    <div className="mx-auto min-h-screen max-w-2xl bg-background pb-24">
      <Player id={id} data={data}>
        {(currentTime, seek) => (
          <div className="space-y-3 px-3 pt-3">
            <PackagePanel
              id={id}
              value={data.package}
              onChange={(value) => setData((previous) => previous && { ...previous, package: value })}
              onToast={setToast}
            />
            <ReviewNotes data={data} showEnglish={showEnglish} onToggleEnglish={() => setShowEnglish(!showEnglish)} />
            {error && <p className="text-xs text-destructive">刷新失败：{error}</p>}
            {!data.editable && (
              <p className="rounded-md bg-amber-50 px-3 py-2 text-xs text-amber-800">任务正在处理中，暂时不能修改</p>
            )}
            {data.paragraphs.map((paragraph, index) => (
              <ParagraphEditor
                key={index}
                index={index}
                paragraph={paragraph}
                showEnglish={showEnglish}
                disabled={!data.editable}
                active={index === lastAtOrBefore(data.paragraphs.map((p) => p.start), currentTime)}
                onSeek={seek}
                onSave={save}
              />
            ))}
          </div>
        )}
      </Player>

      <div className="fixed inset-x-0 bottom-0 border-t bg-background/95 backdrop-blur">
        <div className="mx-auto flex max-w-2xl items-center gap-3 px-3 py-2" style={{ paddingBottom: 'max(0.5rem, env(safe-area-inset-bottom))' }}>
          <div className="min-w-0 flex-1 text-xs text-muted-foreground">
            {data.finalized ? (
              <span className={changedAfterFinal ? 'text-amber-700' : 'text-green-700'}>
                {data.finalized.by} {clock(data.finalized.at)} 已定稿{changedAfterFinal ? '，之后又有修改' : ''}
              </span>
            ) : (
              <span>{data.user?.name ? `${data.user.name}，` : ''}改完点定稿，Word 会发到群里</span>
            )}
          </div>
          <Button size="sm" disabled={finalizing || !data.editable} onClick={finalize}>
            {finalizing ? <Loader2 className="mr-1 h-4 w-4 animate-spin" /> : <CheckCircle2 className="mr-1 h-4 w-4" />}
            {data.finalized ? '重新定稿' : '定稿'}
          </Button>
        </div>
      </div>

      {toast && (
        <div className="fixed inset-x-0 bottom-20 flex justify-center px-4">
          <div className="rounded-full bg-foreground px-4 py-2 text-sm text-background shadow-lg">{toast}</div>
        </div>
      )}
    </div>
  )
}

function Player({ id, data, children }: {
  id: number
  data: ReviewData
  children: (currentTime: number, seek: (t: number) => void) => React.ReactNode
}) {
  const audio = useRef<HTMLAudioElement>(null)
  const [currentTime, setCurrentTime] = useState(0)
  const [duration, setDuration] = useState(data.duration ?? 0)
  const [playing, setPlaying] = useState(false)
  const [rate, setRate] = useState(1)

  const seek = useCallback((t: number) => {
    const element = audio.current
    if (!element) return
    element.currentTime = Math.max(0, t)
    setCurrentTime(element.currentTime)
    element.play().catch(() => undefined)
  }, [])

  const toggle = () => {
    const element = audio.current
    if (!element) return
    if (element.paused) element.play().catch(() => undefined)
    else element.pause()
  }

  const frame = useMemo(() => {
    const index = lastAtOrBefore(data.frames.map((f) => f.t), currentTime)
    return data.frames[Math.max(0, index)]
  }, [data.frames, currentTime])
  const status = STATE_LABELS[data.task.state] ?? data.task.state

  return (
    <>
      <div className="sticky top-0 z-10 border-b bg-background/95 px-3 pb-2 pt-3 backdrop-blur">
        <div className="flex items-start gap-2">
          <div className="min-w-0 flex-1">
            <h1 className="truncate text-base font-semibold">{data.task.title}</h1>
            <p className="truncate text-xs text-muted-foreground">{data.task.subtitle}</p>
          </div>
          <span className={cn('shrink-0 rounded-full px-2 py-0.5 text-xs', STATE_COLORS[data.task.state])}>{status}</span>
        </div>
        {data.has_audio ? (
          <div className="mt-2 flex items-center gap-3">
            {frame && (
              <img
                src={`/api/review/${id}/frames/${frame.name}`}
                alt="直播画面"
                className="aspect-video w-32 shrink-0 rounded bg-muted object-cover sm:w-44"
              />
            )}
            <div className="min-w-0 flex-1 space-y-1">
              <input
                type="range"
                className="w-full accent-primary"
                min={0}
                max={duration || 1}
                step={0.5}
                value={currentTime}
                onChange={(e) => seek(Number(e.target.value))}
              />
              <div className="flex items-center gap-1">
                <Button size="icon" variant="ghost" className="h-8 w-8" onClick={() => seek(currentTime - 5)} aria-label="后退 5 秒">
                  <RotateCcw className="h-4 w-4" />
                </Button>
                <Button size="icon" className="h-9 w-9 rounded-full" onClick={toggle} aria-label={playing ? '暂停' : '播放'}>
                  {playing ? <Pause className="h-4 w-4" /> : <Play className="h-4 w-4" />}
                </Button>
                <Button size="icon" variant="ghost" className="h-8 w-8" onClick={() => seek(currentTime + 5)} aria-label="前进 5 秒">
                  <RotateCw className="h-4 w-4" />
                </Button>
                <span className="ml-1 whitespace-nowrap font-mono text-[11px] tabular-nums text-muted-foreground">
                  {formatSeconds(currentTime)} / {formatSeconds(duration)}
                </span>
                <button
                  className="ml-auto rounded border px-1.5 py-0.5 text-xs tabular-nums"
                  onClick={() => {
                    const next = RATES[(RATES.indexOf(rate) + 1) % RATES.length]
                    setRate(next)
                    if (audio.current) audio.current.playbackRate = next
                  }}
                >
                  {rate}×
                </button>
              </div>
            </div>
            <audio
              ref={audio}
              src={`/api/review/${id}/audio`}
              preload="metadata"
              onTimeUpdate={(e) => setCurrentTime(e.currentTarget.currentTime)}
              onLoadedMetadata={(e) => Number.isFinite(e.currentTarget.duration) && setDuration(e.currentTarget.duration)}
              onPlay={() => setPlaying(true)}
              onPause={() => setPlaying(false)}
              onEnded={() => setPlaying(false)}
            />
          </div>
        ) : (
          <p className="mt-2 text-xs text-muted-foreground">这个任务没有音频</p>
        )}
      </div>
      {children(currentTime, seek)}
    </>
  )
}

function ReviewNotes({ data, showEnglish, onToggleEnglish }: {
  data: ReviewData
  showEnglish: boolean
  onToggleEnglish: () => void
}) {
  return (
    <div className="space-y-2">
      {(data.suspects.length > 0 || data.unknown.length > 0) && (
        <div className="rounded-md bg-amber-50 px-3 py-2 text-xs leading-relaxed text-amber-900">
          <AlertTriangle className="mr-1 inline h-3.5 w-3.5 align-[-2px]" />
          {data.suspects.length > 0 && (
            <span>待核对：{data.suspects.map((s) => `${s.token}（可能是 ${s.candidates.join('/')}）`).join('、')}</span>
          )}
          {data.unknown.length > 0 && (
            <span>{data.suspects.length > 0 ? '；' : ''}未收录的英文词：{data.unknown.slice(0, 20).join('、')}</span>
          )}
        </div>
      )}
      <div className="flex items-center justify-between text-xs text-muted-foreground">
        <span>共 {data.paragraphs.length} 段，点时间从这段开始听</span>
        <button className="rounded border px-2 py-0.5" onClick={onToggleEnglish}>
          {showEnglish ? '隐藏英文' : '显示英文'}
        </button>
      </div>
    </div>
  )
}

// 先用同步的 execCommand（必须在点击的那一刻执行，飞书内置浏览器里最可靠），不行再试剪贴板接口
async function copyText(text: string) {
  const element = document.createElement('textarea')
  element.value = text
  element.setAttribute('readonly', '')
  element.style.position = 'fixed'
  element.style.opacity = '0'
  document.body.appendChild(element)
  element.select()
  element.setSelectionRange(0, text.length)
  let ok = false
  try {
    ok = document.execCommand('copy')
  } catch {
    ok = false
  }
  document.body.removeChild(element)
  if (ok) return
  try {
    await navigator.clipboard.writeText(text)
  } catch {
    throw new Error('浏览器不允许复制，请长按文字手动复制')
  }
}

// 投稿信息：推荐标题、摘要、封面候选，改完自动保存，一键复制
function PackagePanel({ id, value, onChange, onToast }: {
  id: number
  value: PackageData
  onChange: (value: PackageData) => void
  onToast: (text: string) => void
}) {
  const [open, setOpen] = useState(true)
  const [busy, setBusy] = useState(false)

  const save = async (patch: { title?: string; summary?: string }) => {
    try {
      onChange(await call<PackageData>('PUT', `/api/review/${id}/package`, patch))
    } catch (e) {
      onToast(`保存失败：${(e as Error).message}`)
    }
  }

  const regenerate = async () => {
    if (value.status === 'done' && !window.confirm('重新生成标题、摘要和封面候选？改过的标题和摘要会被替换')) return
    setBusy(true)
    try {
      onChange(await call<PackageData>('POST', `/api/review/${id}/package/regenerate`))
      onToast('正在生成，大约需要 1～2 分钟')
    } catch (e) {
      onToast(`生成失败：${(e as Error).message}`)
    } finally {
      setBusy(false)
    }
  }

  const copy = async (text: string, what: string) => {
    try {
      await copyText(text)
      onToast(`已复制${what}`)
    } catch (e) {
      onToast((e as Error).message)
    }
  }

  const running = value.status === 'running'
  return (
    <div className="rounded-lg border bg-muted/30">
      <button className="flex w-full items-center gap-2 px-3 py-2 text-left text-sm font-medium" onClick={() => setOpen(!open)}>
        投稿信息
        {running && <Loader2 className="h-3.5 w-3.5 animate-spin text-muted-foreground" />}
        <span className="truncate text-xs font-normal text-muted-foreground">
          {value.status === 'none' ? '还没有生成' : running ? '生成中…' : '标题、摘要、封面候选'}
        </span>
        <ChevronDown className={cn('ml-auto h-4 w-4 shrink-0 transition-transform', open && 'rotate-180')} />
      </button>
      {open && (
        <div className="space-y-3 border-t px-3 pb-3 pt-2">
          {value.warnings.length > 0 && (
            <p className="rounded-md bg-amber-50 px-2 py-1.5 text-xs leading-relaxed text-amber-900">{value.warnings.join('；')}</p>
          )}
          {(value.titles.length > 0 || value.title) && (
            <CopyField label="标题" value={value.title} rows={2} onSave={(title) => save({ title })} onCopy={(text) => copy(text, '标题')}>
              {value.titles.length > 1 && (
                <div className="mt-1.5 space-y-1">
                  {value.titles.map((title) => (
                    <button
                      key={title}
                      className={cn(
                        'flex w-full items-start gap-1.5 rounded border px-2 py-1 text-left text-[13px] leading-snug',
                        title === value.title ? 'border-primary/60 bg-primary/5' : 'bg-background',
                      )}
                      onClick={() => title !== value.title && save({ title })}
                    >
                      <Check className={cn('mt-0.5 h-3.5 w-3.5 shrink-0', title === value.title ? 'text-primary' : 'text-transparent')} />
                      {title}
                    </button>
                  ))}
                </div>
              )}
            </CopyField>
          )}
          {value.summary && (
            <CopyField label="摘要" value={value.summary} rows={4} onSave={(summary) => save({ summary })} onCopy={(text) => copy(text, '摘要')} />
          )}
          {value.covers.length > 0 && (
            <div>
              <p className="mb-1 text-xs text-muted-foreground">封面候选（点开看原图，长按保存）</p>
              <div className="flex gap-2 overflow-x-auto pb-1">
                {value.covers.map((cover) => (
                  <a key={cover.file} href={cover.url} target="_blank" rel="noreferrer" className="w-40 shrink-0">
                    <img src={cover.url} alt={cover.label} loading="lazy" className="aspect-video w-full rounded bg-muted object-cover" />
                    <span className="mt-0.5 block truncate text-[11px] text-muted-foreground">{cover.label}</span>
                  </a>
                ))}
              </div>
            </div>
          )}
          <Button size="sm" variant="outline" className="h-7" disabled={busy || running} onClick={regenerate}>
            {busy || running ? <Loader2 className="mr-1 h-3.5 w-3.5 animate-spin" /> : <RefreshCw className="mr-1 h-3.5 w-3.5" />}
            {value.status === 'none' ? '生成投稿信息' : '重新生成'}
          </Button>
        </div>
      )}
    </div>
  )
}

function CopyField({ label, value, rows, onSave, onCopy, children }: {
  label: string
  value: string
  rows: number
  onSave: (text: string) => Promise<void>
  onCopy: (text: string) => void
  children?: React.ReactNode
}) {
  const [text, setText] = useState(value)
  const editing = useRef(false)
  // 别人改了、或在候选里点了另一个标题：没在编辑就跟上
  useEffect(() => {
    if (!editing.current) setText(value)
  }, [value])

  return (
    <div>
      <div className="mb-1 flex items-center text-xs text-muted-foreground">
        {label}
        <Button size="sm" variant="ghost" className="ml-auto h-6 px-2 text-xs" onClick={() => onCopy(text)}>
          <Copy className="mr-1 h-3.5 w-3.5" />
          复制
        </Button>
      </div>
      <textarea
        className="block w-full resize-none rounded-md border border-input bg-background px-2.5 py-2 text-[15px] leading-relaxed focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        rows={rows}
        value={text}
        onFocus={() => (editing.current = true)}
        onChange={(e) => setText(e.target.value)}
        onBlur={() => {
          editing.current = false
          if (text.trim() && text !== value) onSave(text)
        }}
      />
      {children}
    </div>
  )
}

type SaveStatus = 'idle' | 'dirty' | 'saving' | 'saved' | 'conflict' | 'error'

function ParagraphEditor({ index, paragraph, showEnglish, disabled, active, onSeek, onSave }: {
  index: number
  paragraph: ReviewParagraph
  showEnglish: boolean
  disabled: boolean
  active: boolean
  onSeek: (t: number) => void
  onSave: (index: number, base: string, text: string) => Promise<void>
}) {
  const [text, setText] = useState(paragraph.zh)
  // 服务端上这一段在本地开始修改之前的内容，保存时一起发过去
  const [base, setBase] = useState(paragraph.zh)
  const [status, setStatus] = useState<SaveStatus>('idle')
  const [message, setMessage] = useState('')
  const textarea = useRef<HTMLTextAreaElement>(null)
  const timer = useRef<number>()
  // 定时保存触发时读最新的内容和 base，不用闭包里的旧值
  const latest = useRef(text)
  latest.current = text
  const baseRef = useRef(base)
  baseRef.current = base
  const dirty = text !== base

  // 服务端内容变了：没在改就直接跟上；正在改就提示冲突
  useEffect(() => {
    if (paragraph.zh === base) return
    if (!dirty) {
      setText(paragraph.zh)
      setBase(paragraph.zh)
      setStatus('idle')
    } else if (status !== 'saving') {
      setStatus('conflict')
    }
  }, [paragraph.zh]) // eslint-disable-line react-hooks/exhaustive-deps

  useLayoutEffect(() => {
    const element = textarea.current
    if (!element) return
    element.style.height = 'auto'
    element.style.height = `${element.scrollHeight + 2}px`
  }, [text, showEnglish])

  const save = async (value: string, against: string) => {
    window.clearTimeout(timer.current)
    if (value === against) return
    if (!value.trim()) {
      setStatus('error')
      setMessage('清空的段落不会保存；要删除这一段请点右边的垃圾桶')
      return
    }
    setStatus('saving')
    try {
      await onSave(index, against, value)
      // 服务端会把段落里的换行接起来
      const saved = value.split('\n').map((line) => line.trim()).join('')
      baseRef.current = saved
      setBase(saved)
      if (latest.current === value) setText(saved)
      setStatus('saved')
    } catch (e) {
      if (e instanceof HttpError && e.status === 409) {
        setStatus('conflict')
      } else {
        setStatus('error')
        setMessage((e as Error).message)
      }
    }
  }

  const remove = async () => {
    if (!window.confirm('删除这一段？')) return
    setStatus('saving')
    try {
      await onSave(index, base, '')
    } catch (e) {
      setStatus(e instanceof HttpError && e.status === 409 ? 'conflict' : 'error')
      setMessage((e as Error).message)
    }
  }

  const kind = /^\s*[Qq]\s*[:：]/.test(text) ? 'Q' : /^\s*[Aa]\s*[:：]/.test(text) ? 'A' : ''

  return (
    <div className={cn('rounded-lg border p-3 transition-colors', active && 'border-primary/60 bg-primary/[0.03]')}>
      <div className="mb-1.5 flex items-center gap-2 text-xs text-muted-foreground">
        {kind && (
          <span className={cn('rounded px-1.5 font-semibold', kind === 'Q' ? 'bg-blue-100 text-blue-700' : 'bg-emerald-100 text-emerald-700')}>
            {kind}
          </span>
        )}
        {paragraph.start !== null && (
          <button className="flex items-center gap-0.5 rounded border px-1.5 font-mono tabular-nums" onClick={() => onSeek(paragraph.start! - 1)}>
            <Play className="h-3 w-3" />
            {formatSeconds(paragraph.start)}
          </button>
        )}
        {paragraph.edited && <span className="truncate">{paragraph.edited.by} 改过</span>}
        <span className="ml-auto shrink-0">
          {status === 'saving' && '保存中…'}
          {status === 'saved' && !dirty && <span className="text-green-700">已保存</span>}
          {status === 'dirty' && dirty && '未保存'}
        </span>
        {!disabled && (
          <button className="shrink-0 p-0.5 text-muted-foreground/70" onClick={remove} aria-label="删除这一段">
            <Trash2 className="h-3.5 w-3.5" />
          </button>
        )}
      </div>
      {showEnglish && paragraph.en && (
        <p className="mb-2 select-text text-[13px] leading-relaxed text-muted-foreground">{paragraph.en}</p>
      )}
      <textarea
        ref={textarea}
        className="block w-full resize-none rounded-md border border-input bg-background px-2.5 py-2 text-[15px] leading-relaxed focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-60"
        value={text}
        disabled={disabled}
        rows={2}
        onChange={(e) => {
          const value = e.target.value
          setText(value)
          if (status === 'conflict') return
          setStatus('dirty')
          window.clearTimeout(timer.current)
          timer.current = window.setTimeout(() => save(latest.current, baseRef.current), SAVE_DELAY)
        }}
        onBlur={() => status !== 'conflict' && save(latest.current, baseRef.current)}
      />
      {status === 'conflict' && (
        <div className="mt-2 space-y-2 rounded-md bg-amber-50 p-2 text-xs text-amber-900">
          <p>
            {paragraph.edited?.by ?? '别人'}刚改过这一段，你的修改还没保存。对方的版本：
          </p>
          <p className="rounded bg-white/70 p-1.5 text-[13px] leading-relaxed">{paragraph.zh}</p>
          <div className="flex gap-2">
            <Button size="sm" className="h-7" onClick={() => {
              setBase(paragraph.zh)
              save(text, paragraph.zh)
            }}>
              用我的覆盖
            </Button>
            <Button size="sm" variant="outline" className="h-7" onClick={() => {
              setText(paragraph.zh)
              setBase(paragraph.zh)
              setStatus('idle')
            }}>
              用对方的
            </Button>
          </div>
        </div>
      )}
      {status === 'error' && <p className="mt-1 text-xs text-destructive">{message}</p>}
    </div>
  )
}
