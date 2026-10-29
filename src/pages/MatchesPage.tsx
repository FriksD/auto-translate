import { useCallback, useEffect, useMemo, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { CalendarCheck, ExternalLink, Loader2, RefreshCw, Star } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Badge, Card, ErrorText, Select } from '@/components/ui/form'
import { api, formatTime, HltvMatch } from '@/lib/api'
import { cn } from '@/lib/utils'

function dayLabel(unixMs: number | null) {
  if (!unixMs) return '进行中'
  return new Date(unixMs).toLocaleDateString('zh-CN', { month: 'long', day: 'numeric', weekday: 'long' })
}

function Stars({ count }: { count: number }) {
  return (
    <span className="inline-flex">
      {Array.from({ length: 5 }, (_, i) => (
        <Star key={i} className={cn('h-3 w-3', i < count ? 'fill-amber-400 text-amber-400' : 'text-slate-200')} />
      ))}
    </span>
  )
}

function MatchRow({ match, onReserve }: { match: HltvMatch; onReserve: (m: HltvMatch) => Promise<void> }) {
  const [busy, setBusy] = useState(false)
  const time = match.live
    ? null
    : match.unix_ms
      ? new Date(match.unix_ms).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', hour12: false })
      : '--:--'
  const teams = [match.team1.name || 'TBD', match.team2.name || 'TBD']

  return (
    <tr className="border-b last:border-0 hover:bg-slate-50">
      <td className="w-16 px-4 py-2.5 font-mono text-sm">
        {match.live ? <Badge className="bg-red-100 text-red-700">LIVE</Badge> : time}
      </td>
      <td className="w-20 px-2 py-2.5">
        <Stars count={match.stars} />
      </td>
      <td className="px-2 py-2.5">
        <span className="font-medium">{teams[0]}</span>
        <span className="mx-2 text-muted-foreground">vs</span>
        <span className="font-medium">{teams[1]}</span>
      </td>
      <td className="px-2 py-2.5 text-sm text-muted-foreground">
        {match.event.name}
        {match.stage && <span className="ml-2 text-xs">· {match.stage}</span>}
      </td>
      <td className="w-12 px-2 py-2.5 text-sm uppercase text-muted-foreground">{match.format}</td>
      <td className="w-36 px-4 py-2.5 text-right">
        <div className="flex items-center justify-end gap-1">
          <a href={match.url} target="_blank" rel="noreferrer" title="在 HLTV 打开">
            <Button variant="ghost" size="icon" className="h-8 w-8">
              <ExternalLink className="h-4 w-4" />
            </Button>
          </a>
          {match.task_id ? (
            <Link to={`/tasks/${match.task_id}`}>
              <Button size="sm" variant="secondary" className="h-8">
                <CalendarCheck className="mr-1.5 h-4 w-4" />
                已预约
              </Button>
            </Link>
          ) : (
            <Button
              size="sm"
              variant="outline"
              className="h-8"
              disabled={busy}
              onClick={async () => {
                setBusy(true)
                try {
                  await onReserve(match)
                } finally {
                  setBusy(false)
                }
              }}
            >
              {busy && <Loader2 className="mr-1.5 h-4 w-4 animate-spin" />}
              预约
            </Button>
          )}
        </div>
      </td>
    </tr>
  )
}

export function MatchesPage() {
  const navigate = useNavigate()
  const [matches, setMatches] = useState<HltvMatch[]>([])
  const [fetchedAt, setFetchedAt] = useState<number | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [query, setQuery] = useState('')
  const [minStars, setMinStars] = useState(0)

  const load = useCallback(async (refresh = false) => {
    setLoading(true)
    setError('')
    try {
      const data = await api.listMatches(refresh)
      setMatches(data.matches)
      setFetchedAt(data.fetched_at)
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    load()
  }, [load])

  const groups = useMemo(() => {
    const q = query.trim().toLowerCase()
    const filtered = matches.filter(
      (m) =>
        m.stars >= minStars &&
        (!q || [m.team1.name, m.team2.name, m.event.name].some((x) => x.toLowerCase().includes(q))),
    )
    const result: { label: string; items: HltvMatch[] }[] = []
    for (const match of filtered) {
      const label = dayLabel(match.live ? null : match.unix_ms)
      const last = result[result.length - 1]
      if (last && last.label === label) last.items.push(match)
      else result.push({ label, items: [match] })
    }
    return result
  }, [matches, query, minStars])

  const reserve = async (match: HltvMatch) => {
    try {
      const task = await api.reserveMatch(match.id)
      setMatches((list) => list.map((m) => (m.id === match.id ? { ...m, task_id: task.id } : m)))
    } catch (e) {
      setError((e as Error).message)
    }
  }

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold">比赛</h1>
          <p className="text-sm text-muted-foreground">
            HLTV 未来 5 天的比赛{fetchedAt ? `，更新于 ${formatTime(fetchedAt)}` : ''}。预约后会立即抓取比赛背景信息。
          </p>
        </div>
        <div className="flex items-center gap-2">
          <Input className="w-56" placeholder="搜索队伍或赛事" value={query} onChange={(e) => setQuery(e.target.value)} />
          <Select className="w-32" value={minStars} onChange={(e) => setMinStars(Number(e.target.value))}>
            <option value={0}>全部星级</option>
            {[1, 2, 3, 4, 5].map((n) => (
              <option key={n} value={n}>
                {n} 星以上
              </option>
            ))}
          </Select>
          <Button variant="outline" onClick={() => load(true)} disabled={loading}>
            <RefreshCw className={cn('mr-2 h-4 w-4', loading && 'animate-spin')} />
            刷新
          </Button>
        </div>
      </div>

      <ErrorText>{error}</ErrorText>
      {loading && matches.length === 0 && (
        <p className="py-12 text-center text-sm text-muted-foreground">
          <Loader2 className="mr-2 inline h-4 w-4 animate-spin" />
          正在通过浏览器打开 HLTV，第一次可能需要几十秒……
        </p>
      )}

      {groups.map((group) => (
        <Card key={group.label} className="p-0">
          <h2 className="border-b bg-slate-50 px-4 py-2 text-sm font-medium">{group.label}</h2>
          <table className="w-full">
            <tbody>
              {group.items.map((match) => (
                <MatchRow key={match.id} match={match} onReserve={reserve} />
              ))}
            </tbody>
          </table>
        </Card>
      ))}
      {!loading && groups.length === 0 && !error && (
        <p className="py-12 text-center text-sm text-muted-foreground">没有符合条件的比赛</p>
      )}
      <p className="text-xs text-muted-foreground">
        已预约的比赛可以在<button className="underline" onClick={() => navigate('/')}>任务</button>页查看。
      </p>
    </div>
  )
}
