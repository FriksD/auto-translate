import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Flag, Headphones, Radio, RefreshCw, Scissors, VolumeX } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Card, ErrorText, Field, Select } from '@/components/ui/form'
import { api, formatSeconds, formatTime, isLive, LiveStatus, parseSeconds, TaskDetail } from '@/lib/api'
import { cn } from '@/lib/utils'

const MAP_STYLES = {
  finished: 'border-slate-300',
  live: 'border-rose-300 bg-rose-50',
  upcoming: 'border-dashed text-muted-foreground',
}

const SOURCE_LABELS: Record<string, string> = { auto: '自动判定', manual: '人工标注', timeout: '超时截取' }
const DATA_SOURCES: Record<string, string> = { bo3: 'bo3.gg', hltv: 'HLTV' }
const STREAM_SOURCES: Record<string, string> = { hltv: 'HLTV', bo3: 'bo3.gg', memory: '同赛事用过' }

function clock(ts: number | undefined) {
  return ts ? formatTime(ts).split(' ').pop() : '—'
}

function SourceLine({ live, onSetBo3 }: { live: LiveStatus; onSetBo3: (value: string) => void }) {
  const [editing, setEditing] = useState(false)
  const [value, setValue] = useState('')
  const snap = live.snapshot
  const bo3 = live.source_ids?.bo3
  return (
    <div className="space-y-1 text-xs text-muted-foreground">
      <p className="flex flex-wrap items-center gap-x-3 gap-y-1">
        <span>
          比分来源：
          {snap?.source ? (
            <a href={snap.source_url} target="_blank" rel="noreferrer" className="hover:underline">
              {DATA_SOURCES[snap.source] ?? snap.source}
            </a>
          ) : (
            '—'
          )}
          {snap?.round_source === 'hltv' && '（回合比分来自 HLTV）'}
        </span>
        <span>
          bo3.gg 对应比赛：
          {bo3 ? (
            <a href={`https://bo3.gg/matches/${bo3.slug}`} target="_blank" rel="noreferrer" className="hover:underline">
              {bo3.slug}
            </a>
          ) : (
            '自动匹配'
          )}
        </span>
        <button className="text-blue-700 hover:underline" onClick={() => setEditing((v) => !v)}>
          {editing ? '取消' : '手动指定'}
        </button>
      </p>
      {editing && (
        <div className="flex max-w-xl gap-2">
          <Input value={value} placeholder="https://bo3.gg/matches/...（留空恢复自动匹配）" onChange={(e) => setValue(e.target.value)} />
          <Button
            size="sm"
            onClick={() => {
              onSetBo3(value)
              setEditing(false)
            }}
          >
            保存
          </Button>
        </div>
      )}
      {snap?.failures?.map((f) => (
        <p key={f.source} className="text-amber-700">
          {DATA_SOURCES[f.source] ?? f.source} 读取失败：{f.error}
        </p>
      ))}
      {live.source_down_since && (
        <p className="text-rose-700">所有比分数据源从 {clock(live.source_down_since)} 起读取失败；比赛结束后请点「比赛已结束」。</p>
      )}
    </div>
  )
}

function WatchSummary({ live, onCheck }: { live: LiveStatus; onCheck: () => void }) {
  const snap = live.snapshot
  if (!snap) return <p className="text-sm text-muted-foreground">尚未开始监视（开赛前自动开始）。</p>
  return (
    <div className="space-y-2 text-sm">
      <div className="flex flex-wrap items-center gap-x-4 gap-y-1">
        <span className="font-semibold">{snap.summary}</span>
        {snap.live?.round && <span className="text-muted-foreground">第 {snap.live.round} 回合</span>}
        {snap.deciding && snap.status === 'live' && <span className="text-rose-700">决胜图</span>}
        <span className="text-muted-foreground">{snap.reason}</span>
      </div>
      <div className="flex flex-wrap gap-2">
        {snap.maps.map((m) => (
          <span key={m.name} className={cn('rounded border px-2 py-0.5 text-xs', MAP_STYLES[m.status ?? 'upcoming'])}>
            {m.name}
            {m.status === 'finished' && (
              <span className="ml-1.5 font-mono">
                {m.team1_score}-{m.team2_score}
              </span>
            )}
            {m.status === 'live' && snap.live && (
              <span className="ml-1.5 font-mono">
                {snap.live.team1}-{snap.live.team2}
              </span>
            )}
          </span>
        ))}
      </div>
      <p className="flex items-center gap-2 text-xs text-muted-foreground">
        上次检查 {clock(snap.checked_at)} · 下次 {clock(snap.next_check_at)}
        <Button variant="ghost" size="sm" className="h-6 px-2" onClick={onCheck}>
          <RefreshCw className="mr-1 h-3 w-3" />
          立即检查
        </Button>
      </p>
    </div>
  )
}

function StreamPicker({ live, disabled, onRecord }: {
  live: LiveStatus
  disabled: boolean
  onRecord: (stream?: { url: string; name?: string; provider?: string }) => void
}) {
  const streams = useMemo(
    () =>
      [...(live.streams ?? [])]
        .filter((s) => s.url)
        .sort((a, b) => Number(b.country === 'Other') - Number(a.country === 'Other') || (b.viewers ?? 0) - (a.viewers ?? 0))
        .slice(0, 20),
    [live.streams],
  )
  const [selected, setSelected] = useState('')
  const [custom, setCustom] = useState('')
  const current = live.stream

  const submit = () => {
    if (custom.trim()) return onRecord({ url: custom.trim(), name: '手动填写' })
    const stream = streams.find((s) => s.url === selected)
    onRecord(stream ? { url: stream.url, name: stream.name, provider: stream.provider } : undefined)
  }

  return (
    <div className="space-y-3">
      <p className="text-sm">
        直播源：
        {current ? (
          <a href={current.url} target="_blank" rel="noreferrer" className="hover:underline">
            {current.name} {current.provider && `（${current.provider}）`} {current.url}
          </a>
        ) : (
          <span className="text-muted-foreground">未选择（开始录制时自动选官方流）</span>
        )}
      </p>
      <div className="grid gap-3 md:grid-cols-[1fr_1fr_auto]">
        <Field label="从列表选择" hint="HLTV、bo3.gg 和同赛事以前用过的直播源">
          <Select value={selected} disabled={disabled} onChange={(e) => setSelected(e.target.value)}>
            <option value="">{current ? '（保持当前）' : '（自动选官方流）'}</option>
            {streams.map((s) => (
              <option key={s.url} value={s.url}>
                {s.name} · {s.provider} · {s.country === 'Other' ? '官方' : s.country}
                {s.source && s.source !== 'hltv' ? ` · ${STREAM_SOURCES[s.source]}` : ` · ${s.viewers ?? 0} 人`}
              </option>
            ))}
          </Select>
        </Field>
        <Field label="或填写直播链接">
          <Input value={custom} disabled={disabled} placeholder="https://www.twitch.tv/..." onChange={(e) => setCustom(e.target.value)} />
        </Field>
        <div className="flex items-end">
          <Button disabled={disabled} onClick={submit}>
            <Radio className="mr-2 h-4 w-4" />
            {live.recording_active ? '切换直播源' : '立即开始录制'}
          </Button>
        </div>
      </div>
    </div>
  )
}

function TranscriptPanel({ live, onPick }: { live: LiveStatus; onPick: (field: 'start' | 'end', seconds: number) => void }) {
  const box = useRef<HTMLDivElement>(null)
  const count = live.transcript.length
  // 新内容到来时，如果原本就停在底部，自动滚到底
  useEffect(() => {
    const el = box.current
    if (el && el.scrollHeight - el.scrollTop - el.clientHeight < 120) el.scrollTop = el.scrollHeight
  }, [count])
  const interview = live.interview
  const detected = live.detection?.started ? live.detection.start : undefined

  if (!count) return <p className="text-sm text-muted-foreground">录制后，每段音频写完会自动转写并显示在这里。</p>
  return (
    <div ref={box} className="max-h-[420px] space-y-1 overflow-y-auto rounded border p-3 text-sm">
      {live.transcript.map((line, i) => {
        const inInterview = interview ? line.end >= interview.start && line.start <= interview.end : detected !== undefined && line.end >= detected
        return (
          <div key={i} className={cn('group flex gap-2', inInterview && 'bg-amber-50')}>
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

export function LivePanel({ task, onChanged }: { task: TaskDetail; onChanged: () => Promise<void> }) {
  const [live, setLive] = useState<LiveStatus | null>(null)
  const [start, setStart] = useState('')
  const [end, setEnd] = useState('')
  const [preview, setPreview] = useState('')
  const [error, setError] = useState('')
  const busy = task.state === 'queued' || task.state === 'processing'

  const load = useCallback(async () => {
    try {
      setLive(await api.liveStatus(task.id))
    } catch (e) {
      setError((e as Error).message)
    }
  }, [task.id])

  const active = isLive(task.state) || !!live?.recording_active
  useEffect(() => {
    load()
    if (!active) return
    const timer = setInterval(load, 3000)
    return () => clearInterval(timer)
  }, [load, active])

  // 自动定位的结果预填到标注框里
  useEffect(() => {
    const source = live?.interview ?? (live?.detection?.started ? { start: live.detection.start, end: undefined } : null)
    if (!source) return
    setStart((v) => v || formatSeconds(source.start))
    if (source.end !== undefined) setEnd((v) => v || formatSeconds(source.end))
  }, [live?.interview, live?.detection?.started, live?.detection?.start])

  const act = async (fn: () => Promise<unknown>) => {
    setError('')
    try {
      await fn()
      await load()
      await onChanged()
    } catch (e) {
      setError((e as Error).message)
    }
  }

  const range = () => {
    const s = parseSeconds(start)
    const e = parseSeconds(end)
    if (s === null || e === null) throw new Error('请填写采访起止时间')
    if (e <= s) throw new Error('结束时间必须晚于开始时间')
    return [s, e] as const
  }

  if (!live) return error ? <ErrorText>{error}</ErrorText> : null
  const detection = live.detection
  const overAt = live.match_over_at && live.recording ? live.match_over_at - live.recording.started_at : null

  return (
    <div className="space-y-5">
      <Card className="space-y-4">
        <WatchSummary live={live} onCheck={() => act(() => api.liveCheck(task.id))} />
        <SourceLine live={live} onSetBo3={(value) => act(() => api.liveSetSource(task.id, value))} />
        <div className="border-t pt-4">
          <StreamPicker live={live} disabled={busy} onRecord={(stream) => act(() => api.liveRecord(task.id, stream))} />
        </div>
      </Card>

      <Card className="space-y-4">
        <div className="flex flex-wrap items-center gap-x-5 gap-y-1 text-sm">
          <span className={cn('font-medium', live.recording_active ? 'text-rose-700' : 'text-muted-foreground')}>
            {live.recording_active ? (live.connected ? '● 录制中' : '● 重连中') : live.recording ? '录制已停止' : '尚未录制'}
          </span>
          {live.recording && (
            <span className="text-muted-foreground">
              已录 {formatSeconds(live.recorded_seconds)} · {live.chunk_count} 段 · 开始于 {clock(live.recording.started_at)}
            </span>
          )}
          {overAt !== null && <span className="text-muted-foreground">比赛结束于录音 {formatSeconds(overAt)}</span>}
        </div>
        {detection?.started && !live.interview && (
          <p className="text-sm text-amber-700">
            检测到采访开始：{detection.interviewee ?? '选手'}，约 {formatSeconds(detection.start)}，
            {detection.pending_end != null
              ? `初步判断在 ${formatSeconds(detection.pending_end)} 结束，等下一次判断确认…`
              : '等待采访结束…'}
          </p>
        )}
        {!!detection?.failures && (
          <p className="text-sm text-rose-700">采访定位的 LLM 已连续失败 {detection.failures} 次，请检查 LLM 设置或手动标注。</p>
        )}
        {detection?.last?.note && !live.interview && (
          <p className="text-xs text-muted-foreground">
            最近一次判断（{clock(detection.checked_at)}）：{detection.last.note}
          </p>
        )}
        {!!live.frame_count && (
          <p className="text-xs text-muted-foreground">
            画面：已存 {live.frame_count} 帧，识别 {live.shot_count ?? 0} 帧
            {live.shot_runs?.length
              ? `；采访镜头 ${live.shot_runs
                  .map((r) => `${formatSeconds(r.start)}–${r.open ? '进行中' : formatSeconds(r.end)}`)
                  .join('、')}`
              : '；暂未出现采访镜头'}
          </p>
        )}
        {live.interview && (
          <p className="text-sm text-emerald-700">
            已截取采访（{SOURCE_LABELS[live.interview.source] ?? live.interview.source}）：
            {formatSeconds(live.interview.start)} – {formatSeconds(live.interview.end)}
          </p>
        )}
        <TranscriptPanel live={live} onPick={(field, seconds) => (field === 'start' ? setStart : setEnd)(formatSeconds(seconds))} />

        {live.recording && (
          <div className="space-y-3 border-t pt-4">
            <p className="text-sm text-muted-foreground">
              自动定位不准时，在上面的转写里悬停一行选择起点/终点（或直接填写，相对录音开始），试听确认后截取。截取时前后各多留几秒。
            </p>
            <div className="flex flex-wrap items-end gap-3">
              <Field label="采访开始" className="w-28">
                <Input value={start} placeholder="mm:ss" onChange={(e) => setStart(e.target.value)} />
              </Field>
              <Field label="采访结束" className="w-28">
                <Input value={end} placeholder="mm:ss" onChange={(e) => setEnd(e.target.value)} />
              </Field>
              <Button
                variant="outline"
                onClick={() => {
                  try {
                    const [s, e] = range()
                    setPreview(api.liveAudioUrl(task.id, s, e))
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
                disabled={busy}
                onClick={() =>
                  act(async () => {
                    const [s, e] = range()
                    if (live.interview && !confirm('已经截取过采访，重新截取会从「获取音频」重跑后续流程，确定？')) return
                    await api.liveCapture(task.id, s, e)
                  })
                }
              >
                <Scissors className="mr-2 h-4 w-4" />
                截取并开始处理
              </Button>
              {(task.state === 'watching' || task.state === 'recording') && !live.match_over_at && (
                <Button
                  variant="ghost"
                  disabled={busy}
                  onClick={() => {
                    if (confirm('确认比赛已经结束？会按赛后流程监听采访（数据源不可用时使用）。')) act(() => api.liveMatchOver(task.id))
                  }}
                >
                  <Flag className="mr-2 h-4 w-4" />
                  比赛已结束
                </Button>
              )}
              {isLive(task.state) && task.state !== 'no_interview' && (
                <Button
                  variant="ghost"
                  disabled={busy}
                  onClick={() => {
                    if (confirm('确认本场没有采访？会停止录制。')) act(() => api.liveNoInterview(task.id))
                  }}
                >
                  <VolumeX className="mr-2 h-4 w-4" />
                  本场无采访
                </Button>
              )}
            </div>
            {preview && <audio key={preview} controls autoPlay src={preview} className="w-full" />}
          </div>
        )}
      </Card>
      <ErrorText>{error}</ErrorText>
    </div>
  )
}
