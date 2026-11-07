import { useCallback, useEffect, useRef, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { Headphones, Loader2, Pause, Play, Plus, Radio, Scissors, Trash2 } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Badge, Card, CardTitle, Checkbox, ErrorText, Field, Select } from '@/components/ui/form'
import {
  api,
  formatSeconds,
  formatTime,
  HltvEvent,
  Monitor,
  MonitorStreamConfig,
  MonitorStreamStatus,
  parseSeconds,
  STATE_COLORS,
  STATE_LABELS,
} from '@/lib/api'
import { cn } from '@/lib/utils'

const STREAM_STATUS: Record<string, { label: string; className: string }> = {
  recording: { label: '● 录制中', className: 'text-rose-700' },
  offline: { label: '未开播', className: 'text-muted-foreground' },
  starting: { label: '正在探测…', className: 'text-muted-foreground' },
  replay: { label: '拉到的是回放，稍后重试', className: 'text-amber-700' },
  unknown: { label: '探测失败，尝试直接录制', className: 'text-amber-700' },
  stopped: { label: '没在监视', className: 'text-muted-foreground' },
}

const DOMESTIC = ['bilibili.com', 'huya.com', 'douyu.com', 'douyin.com']

function defaultProxy(url: string) {
  return !DOMESTIC.some((host) => url.toLowerCase().includes(host))
}

function clock(ts: number | null | undefined) {
  return ts ? formatTime(ts).split(' ').pop() : '—'
}

// ── 列表 ─────────────────────────────────────────────────────────────────────

function StreamBadges({ monitor }: { monitor: Monitor }) {
  return (
    <div className="space-y-0.5 text-xs">
      {monitor.stream_status.map((s) => {
        const style = STREAM_STATUS[s.status] ?? STREAM_STATUS.starting
        return (
          <p key={s.url} className="flex flex-wrap gap-x-2">
            <span className={style.className}>{style.label}</span>
            <span className="text-muted-foreground">
              {s.label || s.platform} · {s.proxy ? '走代理' : '直连'}
              {s.title && ` · ${s.title}`}
            </span>
          </p>
        )
      })}
    </div>
  )
}

function StreamFields({ streams, onChange }: { streams: MonitorStreamConfig[]; onChange: (s: MonitorStreamConfig[]) => void }) {
  const rows = streams.length ? streams : [{ url: '' }]
  const update = (index: number, patch: Partial<MonitorStreamConfig>) =>
    onChange(rows.map((row, i) => (i === index ? { ...row, ...patch } : row)))
  return (
    <div className="space-y-2">
      {rows.map((row, i) => (
        <div key={i} className="grid items-end gap-2 md:grid-cols-[1fr_9rem_auto_auto]">
          <Field label={i === 0 ? '直播链接' : '第二条直播链接'}>
            <Input
              value={row.url}
              placeholder="https://live.bilibili.com/35、YouTube / Twitch 频道链接"
              onChange={(e) => update(i, { url: e.target.value, proxy: row.proxy ?? null })}
            />
          </Field>
          <Field label="名称">
            <Input value={row.label ?? ''} placeholder="主舞台" onChange={(e) => update(i, { label: e.target.value })} />
          </Field>
          <div className="pb-2">
            <Checkbox
              label="走代理"
              checked={row.proxy ?? defaultProxy(row.url)}
              onChange={(checked) => update(i, { proxy: checked })}
            />
          </div>
          <div className="pb-1">
            {rows.length > 1 && (
              <Button variant="ghost" size="icon" title="删掉这条" onClick={() => onChange(rows.filter((_, j) => j !== i))}>
                <Trash2 className="h-4 w-4" />
              </Button>
            )}
          </div>
        </div>
      ))}
      {rows.length < 2 && (
        <Button variant="ghost" size="sm" onClick={() => onChange([...rows, { url: '' }])}>
          <Plus className="mr-1 h-4 w-4" />
          加第二个直播间
        </Button>
      )}
      <p className="text-xs text-muted-foreground">
        国内平台（B 站、虎牙、斗鱼、抖音）默认直连，其他默认走设置里的代理。直播间没开播时会每分钟探测一次，开播后自动开始录。
      </p>
    </div>
  )
}

function CreateMonitor({ onCreated }: { onCreated: (m: Monitor) => void }) {
  const [events, setEvents] = useState<HltvEvent[]>([])
  const [eventsError, setEventsError] = useState('')
  const [eventKey, setEventKey] = useState('')
  const [eventName, setEventName] = useState('')
  const [title, setTitle] = useState('')
  const [streams, setStreams] = useState<MonitorStreamConfig[]>([{ url: '' }])
  const [start, setStart] = useState(true)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  useEffect(() => {
    api
      .monitorEvents()
      .then((r) => {
        setEvents(r.events)
        setEventsError(r.error ?? '')
      })
      .catch((e) => setEventsError((e as Error).message))
  }, [])

  const pick = (key: string) => {
    setEventKey(key)
    const event = events.find((e) => String(e.id ?? e.name) === key)
    if (event) {
      setEventName(event.name)
      setTitle((t) => t || event.name)
    }
  }

  const submit = async () => {
    setError('')
    const filled = streams.filter((s) => s.url.trim())
    if (!filled.length) return setError('请至少填写一条直播链接')
    if (!eventName.trim()) return setError('请选择或填写赛事名（英文，用来识别参赛队伍）')
    const event = events.find((e) => String(e.id ?? e.name) === eventKey)
    setBusy(true)
    try {
      const created = await api.createMonitor({
        title: title.trim() || eventName.trim(),
        event_name: eventName.trim(),
        event_id: event?.id != null ? String(event.id) : null,
        streams: filled.map((s) => ({ ...s, url: s.url.trim(), proxy: s.proxy ?? null })),
        enabled: start,
      })
      onCreated(created)
      setStreams([{ url: '' }])
      setEventKey('')
      setEventName('')
      setTitle('')
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <Card className="space-y-4">
      <CardTitle className="mb-0">新建直播间监视</CardTitle>
      <div className="grid gap-3 md:grid-cols-3">
        <Field label="从 HLTV 赛程选赛事" hint={eventsError || '选了会自动填英文赛事名'}>
          <Select value={eventKey} onChange={(e) => pick(e.target.value)}>
            <option value="">（不选，手动填写）</option>
            {events.map((e) => (
              <option key={String(e.id ?? e.name)} value={String(e.id ?? e.name)}>
                {'★'.repeat(e.stars)} {e.name}（{e.matches} 场）
              </option>
            ))}
          </Select>
        </Field>
        <Field label="赛事名（英文）" hint="用来识别参赛队伍，例如 IEM Beijing 2026">
          <Input value={eventName} onChange={(e) => setEventName(e.target.value)} />
        </Field>
        <Field label="显示名称" hint="留空用赛事名">
          <Input value={title} placeholder="IEM 北京" onChange={(e) => setTitle(e.target.value)} />
        </Field>
      </div>
      <StreamFields streams={streams} onChange={setStreams} />
      <div className="flex items-center gap-4">
        <Button disabled={busy} onClick={submit}>
          {busy ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : <Plus className="mr-2 h-4 w-4" />}
          创建
        </Button>
        <Checkbox label="创建后立即开始监视" checked={start} onChange={setStart} />
      </div>
      <ErrorText>{error}</ErrorText>
    </Card>
  )
}

export function MonitorsPage() {
  const [monitors, setMonitors] = useState<Monitor[] | null>(null)
  const [error, setError] = useState('')
  const [creating, setCreating] = useState(false)

  const load = useCallback(async () => {
    try {
      setMonitors(await api.listMonitors())
      setError('')
    } catch (e) {
      setError((e as Error).message)
    }
  }, [])

  useEffect(() => {
    load()
    const timer = setInterval(load, 5000)
    return () => clearInterval(timer)
  }, [load])

  const toggle = async (m: Monitor) => {
    try {
      await api.updateMonitor(m.id, { enabled: !m.enabled })
      await load()
    } catch (e) {
      setError((e as Error).message)
    }
  }

  return (
    <div className="space-y-5">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-xl font-semibold">直播间</h1>
          <p className="text-sm text-muted-foreground">
            一个赛事建一个，贴上官方直播间链接。监视期间一直录、一直识别，发现现场采访就截下来，每段采访一个任务。
          </p>
        </div>
        <Button variant={creating ? 'outline' : 'default'} onClick={() => setCreating((v) => !v)}>
          <Plus className="mr-2 h-4 w-4" />
          {creating ? '收起' : '新建'}
        </Button>
      </div>
      {creating && (
        <CreateMonitor
          onCreated={async () => {
            setCreating(false)
            await load()
          }}
        />
      )}
      <ErrorText>{error}</ErrorText>
      {monitors === null ? (
        <Loader2 className="mx-auto mt-10 h-6 w-6 animate-spin" />
      ) : monitors.length === 0 ? (
        <Card className="py-10 text-center text-sm text-muted-foreground">还没有直播间监视，点「新建」添加。</Card>
      ) : (
        <div className="space-y-3">
          {monitors.map((m) => (
            <Card key={m.id} className="flex flex-wrap items-start justify-between gap-4">
              <div className="min-w-0 flex-1 space-y-1.5">
                <div className="flex flex-wrap items-center gap-2">
                  <Link to={`/monitors/${m.id}`} className="font-semibold hover:underline">
                    {m.title}
                  </Link>
                  {m.event_name && m.event_name !== m.title && (
                    <span className="text-sm text-muted-foreground">{m.event_name}</span>
                  )}
                  <Badge className={m.on ? 'bg-rose-100 text-rose-700' : 'bg-slate-100 text-slate-600'}>
                    {m.on ? (m.enabled ? '监视中' : '预约时段内监视中') : '已暂停'}
                  </Badge>
                </div>
                <StreamBadges monitor={m} />
                {m.tasks.length > 0 && (
                  <p className="text-xs text-muted-foreground">
                    已截取 {m.tasks.length} 段采访，最近：
                    <Link to={`/tasks/${m.tasks[0].id}`} className="hover:underline">
                      {m.tasks[0].title}
                    </Link>
                  </p>
                )}
              </div>
              <div className="flex gap-2">
                <Button variant="outline" onClick={() => toggle(m)}>
                  {m.enabled ? <Pause className="mr-2 h-4 w-4" /> : <Play className="mr-2 h-4 w-4" />}
                  {m.enabled ? '暂停' : '开始监视'}
                </Button>
                <Link to={`/monitors/${m.id}`}>
                  <Button variant="ghost">详情</Button>
                </Link>
              </div>
            </Card>
          ))}
        </div>
      )}
    </div>
  )
}

// ── 详情 ─────────────────────────────────────────────────────────────────────

function StreamTranscript({ stream, onPick }: { stream: MonitorStreamStatus; onPick: (field: 'start' | 'end', t: number) => void }) {
  const box = useRef<HTMLDivElement>(null)
  const lines = stream.transcript ?? []
  const count = lines.length
  useEffect(() => {
    const el = box.current
    if (el && el.scrollHeight - el.scrollTop - el.clientHeight < 120) el.scrollTop = el.scrollHeight
  }, [count])
  const active = stream.detection.active
  const runs = stream.shot_runs
  if (!count) return <p className="text-sm text-muted-foreground">开播录制后，每段音频写完会自动转写并显示在这里。</p>
  return (
    <div ref={box} className="max-h-[420px] space-y-1 overflow-y-auto rounded border p-3 text-sm">
      {lines.map((line, i) => {
        const onCamera = runs.some((r) => line.end >= r.start && line.start <= r.end)
        const inActive = active && line.end >= active.start
        return (
          <div key={i} className={cn('group flex gap-2', inActive ? 'bg-amber-50' : onCamera && 'bg-sky-50')}>
            <span className="shrink-0 font-mono text-xs leading-5 text-muted-foreground">{formatSeconds(line.start)}</span>
            <span className="flex-1">{line.text}</span>
            <span className="invisible shrink-0 space-x-1 group-hover:visible">
              <button className="text-xs text-blue-700 hover:underline" onClick={() => onPick('start', line.start)}>
                设为起点
              </button>
              <button className="text-xs text-blue-700 hover:underline" onClick={() => onPick('end', line.end)}>
                设为终点
              </button>
            </span>
          </div>
        )
      })}
    </div>
  )
}

function StreamCard({ monitor, index, onChanged }: { monitor: Monitor; index: number; onChanged: () => Promise<void> }) {
  const stream = monitor.stream_status[index]
  const navigate = useNavigate()
  const [start, setStart] = useState('')
  const [end, setEnd] = useState('')
  const [who, setWho] = useState('')
  const [preview, setPreview] = useState('')
  const [error, setError] = useState('')
  const style = STREAM_STATUS[stream.status] ?? STREAM_STATUS.starting
  const active = stream.detection.active

  const range = () => {
    const s = parseSeconds(start)
    const e = parseSeconds(end)
    if (s === null || e === null) throw new Error('请填写采访起止时间')
    if (e <= s) throw new Error('结束时间必须晚于开始时间')
    return [s, e] as const
  }

  return (
    <Card className="space-y-4">
      <div className="space-y-1">
        <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-sm">
          <span className="font-semibold">{stream.label || stream.platform}</span>
          <span className={cn('font-medium', style.className)}>
            {stream.recording && !stream.connected ? '● 重连中' : style.label}
          </span>
          <a href={stream.url} target="_blank" rel="noreferrer" className="text-muted-foreground hover:underline">
            {stream.url}
          </a>
          <span className="text-muted-foreground">{stream.proxy ? '走代理' : '直连'}</span>
        </div>
        <p className="text-xs text-muted-foreground">
          {stream.title && `${stream.title} · `}
          {stream.status_at && `状态更新于 ${clock(stream.status_at)}`}
          {stream.origin && ` · 本轮录制开始于 ${formatTime(stream.origin)}，${stream.chunk_count} 段`}
        </p>
        {stream.error && <p className="text-xs text-amber-700">{stream.error}</p>}
      </div>

      {active ? (
        <p className="text-sm text-amber-700">
          正在采访：{active.interviewees.join('、') || '选手'}
          {active.team && `（${active.team}）`}，约 {formatSeconds(active.start)} 开始，
          {active.pending_end != null ? `初步判断在 ${formatSeconds(active.pending_end)} 结束，等下一次判断确认…` : '等待采访结束…'}
        </p>
      ) : stream.detection.trigger_at != null ? (
        <p className="text-sm text-sky-700">{formatSeconds(stream.detection.trigger_at)} 出现采访迹象，正在让 LLM 判断…</p>
      ) : null}
      {!!stream.detection.failures && (
        <p className="text-sm text-rose-700">采访定位的 LLM 已连续失败 {stream.detection.failures} 次，请检查 LLM 设置或手动标注。</p>
      )}
      {stream.shot_runs.length > 0 && (
        <p className="text-xs text-muted-foreground">
          采访镜头：
          {stream.shot_runs.map((r) => `${formatSeconds(r.start)}–${r.open ? '进行中' : formatSeconds(r.end)}`).join('、')}
        </p>
      )}

      <StreamTranscript stream={stream} onPick={(field, t) => (field === 'start' ? setStart : setEnd)(formatSeconds(t))} />

      {stream.origin && (
        <div className="space-y-3 border-t pt-4">
          <p className="text-sm text-muted-foreground">
            漏掉的采访可以手动截：在转写里悬停一行选择起点/终点（时间相对本轮录制开始），试听确认后生成任务。
          </p>
          <div className="flex flex-wrap items-end gap-3">
            <Field label="采访开始" className="w-28">
              <Input value={start} placeholder="mm:ss" onChange={(e) => setStart(e.target.value)} />
            </Field>
            <Field label="采访结束" className="w-28">
              <Input value={end} placeholder="mm:ss" onChange={(e) => setEnd(e.target.value)} />
            </Field>
            <Field label="受访者（可不填）" className="w-48">
              <Input value={who} placeholder="ZywOo, apEX" onChange={(e) => setWho(e.target.value)} />
            </Field>
            <Button
              variant="outline"
              onClick={() => {
                try {
                  const [s, e] = range()
                  setPreview(api.monitorAudioUrl(monitor.id, index, s, e))
                  setError('')
                } catch (err) {
                  setError((err as Error).message)
                }
              }}
            >
              <Headphones className="mr-2 h-4 w-4" />
              试听
            </Button>
            <Button
              onClick={async () => {
                setError('')
                try {
                  const [s, e] = range()
                  const names = who.split(/[,，、]/).map((n) => n.trim()).filter(Boolean)
                  const task = await api.monitorCapture(monitor.id, index, s, e, names)
                  await onChanged()
                  navigate(`/tasks/${task.id}`)
                } catch (err) {
                  setError((err as Error).message)
                }
              }}
            >
              <Scissors className="mr-2 h-4 w-4" />
              截取并生成任务
            </Button>
          </div>
          {preview && <audio key={preview} controls autoPlay src={preview} className="w-full" />}
        </div>
      )}
      <ErrorText>{error}</ErrorText>
    </Card>
  )
}

function EditMonitor({ monitor, onSaved }: { monitor: Monitor; onSaved: () => Promise<void> }) {
  const [title, setTitle] = useState(monitor.title)
  const [eventName, setEventName] = useState(monitor.event_name ?? '')
  const [streams, setStreams] = useState<MonitorStreamConfig[]>(monitor.streams)
  const [error, setError] = useState('')
  return (
    <Card className="space-y-4">
      <div className="grid gap-3 md:grid-cols-2">
        <Field label="显示名称">
          <Input value={title} onChange={(e) => setTitle(e.target.value)} />
        </Field>
        <Field label="赛事名（英文）" hint="用来识别参赛队伍">
          <Input value={eventName} onChange={(e) => setEventName(e.target.value)} />
        </Field>
      </div>
      <StreamFields streams={streams} onChange={setStreams} />
      <Button
        onClick={async () => {
          setError('')
          try {
            await api.updateMonitor(monitor.id, {
              title,
              event_name: eventName,
              streams: streams.filter((s) => s.url.trim()).map((s) => ({ ...s, url: s.url.trim() })),
            })
            await onSaved()
          } catch (e) {
            setError((e as Error).message)
          }
        }}
      >
        保存
      </Button>
      <ErrorText>{error}</ErrorText>
    </Card>
  )
}

export function MonitorPage() {
  const { id } = useParams()
  const monitorId = Number(id)
  const navigate = useNavigate()
  const [monitor, setMonitor] = useState<Monitor | null>(null)
  const [editing, setEditing] = useState(false)
  const [error, setError] = useState('')

  const load = useCallback(async () => {
    try {
      setMonitor(await api.getMonitor(monitorId))
      setError('')
    } catch (e) {
      setError((e as Error).message)
    }
  }, [monitorId])

  useEffect(() => {
    load()
    const timer = setInterval(load, 4000)
    return () => clearInterval(timer)
  }, [load])

  if (!monitor) return error ? <ErrorText>{error}</ErrorText> : <Loader2 className="mx-auto mt-20 h-6 w-6 animate-spin" />

  const act = async (fn: () => Promise<unknown>) => {
    setError('')
    try {
      await fn()
      await load()
    } catch (e) {
      setError((e as Error).message)
    }
  }

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div className="space-y-1">
          <div className="flex items-center gap-3">
            <h1 className="text-xl font-semibold">{monitor.title}</h1>
            <Badge className={monitor.on ? 'bg-rose-100 text-rose-700' : 'bg-slate-100 text-slate-600'}>
              {monitor.on ? '监视中' : '已暂停'}
            </Badge>
          </div>
          <p className="text-sm text-muted-foreground">
            {monitor.event_name || '未填写赛事名'}
            {monitor.windows.length > 0 &&
              ` · 预约时段：${monitor.windows.map((w) => `${formatTime(w.start)}–${clock(w.end)}`).join('、')}`}
          </p>
        </div>
        <div className="flex gap-2">
          <Button onClick={() => act(() => api.updateMonitor(monitor.id, { enabled: !monitor.enabled }))}>
            {monitor.enabled ? <Pause className="mr-2 h-4 w-4" /> : <Radio className="mr-2 h-4 w-4" />}
            {monitor.enabled ? '暂停监视' : '开始监视'}
          </Button>
          <Button variant="outline" onClick={() => setEditing((v) => !v)}>
            {editing ? '收起' : '修改链接'}
          </Button>
          <Button
            variant="ghost"
            size="icon"
            title="删除"
            onClick={() => {
              if (confirm('删除这个直播间监视？已经截取的采访任务不受影响。'))
                act(async () => {
                  await api.deleteMonitor(monitor.id)
                  navigate('/monitors')
                })
            }}
          >
            <Trash2 className="h-4 w-4" />
          </Button>
        </div>
      </div>
      <ErrorText>{error}</ErrorText>
      {editing && (
        <EditMonitor
          monitor={monitor}
          onSaved={async () => {
            setEditing(false)
            await load()
          }}
        />
      )}

      {monitor.stream_status.map((stream, index) => (
        <StreamCard key={stream.url} monitor={monitor} index={index} onChanged={load} />
      ))}

      <div className="grid gap-5 md:grid-cols-2">
        <Card>
          <CardTitle>截到的采访</CardTitle>
          {monitor.tasks.length === 0 ? (
            <p className="text-sm text-muted-foreground">还没有</p>
          ) : (
            <ul className="space-y-1.5 text-sm">
              {monitor.tasks.map((t) => (
                <li key={t.id} className="flex items-center gap-2">
                  <Badge className={STATE_COLORS[t.state] ?? 'bg-slate-100'}>{STATE_LABELS[t.state] ?? t.state}</Badge>
                  <Link to={`/tasks/${t.id}`} className="truncate hover:underline">
                    {t.title}
                  </Link>
                  <span className="ml-auto shrink-0 text-xs text-muted-foreground">{clock(t.created_at)}</span>
                </li>
              ))}
            </ul>
          )}
        </Card>
        <Card>
          <CardTitle>动态</CardTitle>
          <div className="max-h-80 space-y-1 overflow-y-auto font-mono text-xs">
            {[...monitor.events].reverse().map((e) => (
              <p key={e.id} className={cn(e.level === 'error' && 'text-red-700', e.level === 'warning' && 'text-amber-700')}>
                <span className="mr-2 text-muted-foreground">{formatTime(e.ts)}</span>
                {e.message}
              </p>
            ))}
          </div>
        </Card>
      </div>
    </div>
  )
}
