import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { Card, ErrorText } from '@/components/ui/form'
import { api, formatSeconds, formatTime, LiveStatus, TaskDetail } from '@/lib/api'

const KIND_LABELS: Record<string, string> = {
  just_played: '赛后采访',
  between_maps: '图间采访',
  upcoming: '赛前采访',
  general: '和具体比赛无关',
  unknown: '不确定',
}

const SOURCE_LABELS: Record<string, string> = {
  auto: '自动判定',
  camera: '镜头切走',
  manual: '人工标注',
  timeout: '超时截取',
}

/** 直播间监视截下来的采访：来自哪个直播间、什么时间，事后识别的受访者和对应比赛。 */
export function InterviewPanel({ task }: { task: TaskDetail }) {
  const [live, setLive] = useState<LiveStatus | null>(null)
  const [error, setError] = useState('')
  const monitor = task.context.monitor
  const identified = task.context.identified

  useEffect(() => {
    api.liveStatus(task.id).then(setLive).catch((e) => setError((e as Error).message))
  }, [task.id])

  const interview = live?.interview
  const origin = live?.recording?.started_at
  return (
    <div className="space-y-4 text-sm">
      <Card className="space-y-1.5">
        <p>
          来自直播间：
          {monitor ? (
            <Link to={`/monitors/${monitor.id}`} className="text-blue-700 hover:underline">
              {monitor.event_name || `#${monitor.id}`}
            </Link>
          ) : (
            '—'
          )}
          {live?.stream?.url && (
            <a href={live.stream.url} target="_blank" rel="noreferrer" className="ml-2 text-muted-foreground hover:underline">
              {live.stream.url}
            </a>
          )}
        </p>
        {interview && origin && (
          <p className="text-muted-foreground">
            采访时间：{formatTime(origin + interview.start)} – {formatTime(origin + interview.end).split(' ').pop()}
            （{formatSeconds(interview.end - interview.start)}，{SOURCE_LABELS[interview.source] ?? interview.source}）
          </p>
        )}
        {identified ? (
          <p>
            识别结果：{identified.interviewees.join('、') || '受访者未知'}
            {identified.team && `（${identified.team}）`} · {KIND_LABELS[identified.about_match] ?? identified.about_match}
            {identified.opponent && ` · 对手 ${identified.opponent}`}
            {identified.note && <span className="text-muted-foreground"> · {identified.note}</span>}
          </p>
        ) : (
          <p className="text-muted-foreground">「抓取背景信息」这一步会识别受访者、战队和对应的比赛。</p>
        )}
        <p className="text-xs text-muted-foreground">
          识别错了可以在「背景信息」里改队伍和选手，再从「英文清洗」重跑；截取范围不对，到直播间页重新手动截取。
        </p>
      </Card>
      {live && live.transcript.length > 0 && (
        <Card>
          <p className="mb-2 font-medium">直播滚动转写（截取范围内）</p>
          <div className="max-h-[420px] space-y-1 overflow-y-auto">
            {live.transcript.map((line, i) => (
              <p key={i}>
                <span className="mr-2 font-mono text-xs text-muted-foreground">{formatSeconds(line.start)}</span>
                {line.text}
              </p>
            ))}
          </div>
        </Card>
      )}
      <ErrorText>{error}</ErrorText>
    </div>
  )
}
