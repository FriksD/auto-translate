import { ExternalLink } from 'lucide-react'
import { Card } from '@/components/ui/form'
import { formatMatchTime, TaskContext } from '@/lib/api'

const STATUS_LABELS = { upcoming: '未开始', live: '进行中', over: '已结束' }

export function MatchCard({ context }: { context: TaskContext }) {
  const match = context.match
  if (!match) return null
  const names = (context.teams ?? []).map((t) => t.name)
  const [team1, team2] = [names[0] ?? 'TBD', names[1] ?? 'TBD']
  const streams = [...match.streams].sort((a, b) => (b.viewers ?? 0) - (a.viewers ?? 0)).slice(0, 6)

  return (
    <Card className="space-y-3 text-sm">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <div className="space-x-2">
          <span className="font-semibold">{match.event}</span>
          <span className="text-muted-foreground">{match.format}</span>
        </div>
        <a href={match.url} target="_blank" rel="noreferrer" className="inline-flex items-center gap-1 text-muted-foreground hover:text-foreground">
          HLTV <ExternalLink className="h-3.5 w-3.5" />
        </a>
      </div>
      <div className="flex flex-wrap items-center gap-x-6 gap-y-1">
        <span>
          {team1}
          {match.status !== 'upcoming' && match.score[0] !== null && (
            <span className="mx-2 font-mono font-semibold">
              {match.score[0]} : {match.score[1]}
            </span>
          )}
          {match.status === 'upcoming' && <span className="mx-2 text-muted-foreground">vs</span>}
          {team2}
        </span>
        <span className="text-muted-foreground">
          {formatMatchTime(match.unix_ms)} · {STATUS_LABELS[match.status]}
        </span>
      </div>
      {match.maps.length > 0 && (
        <div className="flex flex-wrap gap-2">
          {match.maps.map((m) => (
            <span key={m.name} className="rounded border px-2 py-0.5 text-xs">
              {m.name}
              {m.team1_score !== null && (
                <span className="ml-1.5 font-mono">
                  {m.team1_score}-{m.team2_score}
                </span>
              )}
            </span>
          ))}
        </div>
      )}
      {streams.length > 0 && (
        <div className="text-xs text-muted-foreground">
          直播源：
          {streams.map((s, i) => (
            <span key={`${s.provider}-${s.name}-${i}`}>
              {i > 0 && '、'}
              <a href={s.url} target="_blank" rel="noreferrer" className="hover:text-foreground hover:underline">
                {s.name}（{s.provider}
                {s.country && s.country !== 'Other' ? ` · ${s.country}` : ''}）
              </a>
            </span>
          ))}
        </div>
      )}
    </Card>
  )
}
