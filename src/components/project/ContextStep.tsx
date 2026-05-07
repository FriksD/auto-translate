import { useState } from 'react'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Textarea } from '@/components/ui/textarea'
import { Loader2, Plus, X } from 'lucide-react'

interface ContextStepProps {
  initialContext?: {
    teams?: any[]
    players?: any[]
    customContext?: string
  }
  onSubmit: (context: any) => void
  onSkip: () => void
}

export function ContextStep({ initialContext, onSubmit, onSkip }: ContextStepProps) {
  const [teams, setTeams] = useState<any[]>(initialContext?.teams || [])
  const [players, setPlayers] = useState<any[]>(initialContext?.players || [])
  const [customContext, setCustomContext] = useState(initialContext?.customContext || '')
  const [searchQuery, setSearchQuery] = useState('')
  const [searchType, setSearchType] = useState<'team' | 'player'>('team')
  const [isSearching, setIsSearching] = useState(false)

  const normalizeText = (value: unknown) => {
    if (typeof value === 'string') return value.trim()
    if (value === null || value === undefined) return ''
    return String(value).trim()
  }

  const buildPlayerKey = (name: string, team?: string) =>
    `${normalizeText(name).toLowerCase()}::${normalizeText(team || '').toLowerCase()}`

  const uniqueManualPlayers: Array<{ name: string; team?: string; raw: any }> = []
  const seenManualPlayerKeys = new Set<string>()
  for (const player of players) {
    const name = normalizeText(player?.name)
    const team = normalizeText(player?.team) || undefined
    if (!name) continue
    const key = buildPlayerKey(name, team)
    if (seenManualPlayerKeys.has(key)) continue
    seenManualPlayerKeys.add(key)
    uniqueManualPlayers.push({ name, team, raw: { ...player, name, team } })
  }

  const rosterPlayers: Array<{ name: string; team?: string }> = []
  for (const team of teams) {
    const teamName = normalizeText(team?.name) || undefined
    const roster = Array.isArray(team?.players) ? team.players : []
    for (const nick of roster) {
      const name = normalizeText(nick)
      if (!name) continue
      rosterPlayers.push({ name, team: teamName })
    }
  }

  const mergedPlayersForView: Array<{ name: string; team?: string; isManual: boolean }> = []
  const mergedKeys = new Set<string>()
  for (const player of uniqueManualPlayers) {
    const key = buildPlayerKey(player.name, player.team)
    mergedKeys.add(key)
    mergedPlayersForView.push({ name: player.name, team: player.team, isManual: true })
  }
  for (const player of rosterPlayers) {
    const key = buildPlayerKey(player.name, player.team)
    if (mergedKeys.has(key)) continue
    mergedKeys.add(key)
    mergedPlayersForView.push({ name: player.name, team: player.team, isManual: false })
  }

  const mergedPlayersForSubmit = mergedPlayersForView.map((player) => ({
    name: player.name,
    team: player.team,
  }))

  const coachTags: Array<{ coach: string; team: string }> = []
  const seenCoachKeys = new Set<string>()
  for (const team of teams) {
    const teamName = normalizeText(team?.name)
    const coach = normalizeText(team?.stats?.coach)
    if (!teamName || !coach) continue
    const key = `${coach.toLowerCase()}::${teamName.toLowerCase()}`
    if (seenCoachKeys.has(key)) continue
    seenCoachKeys.add(key)
    coachTags.push({ coach, team: teamName })
  }

  const handleSearch = async () => {
    if (!searchQuery.trim()) return

    setIsSearching(true)
    try {
      const result = await window.electronAPI.getHLTVInfo(searchQuery, searchType)
      if (result.error) {
        alert(result.error)
      } else {
        if (searchType === 'team') {
          const teamName = normalizeText(result?.name)
          const dedupedRoster = Array.from(
            new Set(
              (Array.isArray(result?.players) ? result.players : [])
                .map((nick) => normalizeText(nick))
                .filter(Boolean)
            )
          )

          setTeams((prev) => {
            if (teamName && prev.some((team) => normalizeText(team?.name).toLowerCase() === teamName.toLowerCase())) {
              return prev
            }
            return [
              ...prev,
              {
                ...result,
                name: teamName || result?.name,
                players: dedupedRoster,
                stats: {
                  ...(result?.stats || {}),
                  coach: normalizeText(result?.stats?.coach) || undefined,
                },
              },
            ]
          })
        } else {
          const playerName = normalizeText(result?.name)
          const playerTeam = normalizeText(result?.team) || undefined
          if (playerName) {
            const incomingKey = buildPlayerKey(playerName, playerTeam)
            setPlayers((prev) => {
              const exists = prev.some((player) => {
                const name = normalizeText(player?.name)
                const team = normalizeText(player?.team) || undefined
                return buildPlayerKey(name, team) === incomingKey
              })
              if (exists) return prev
              return [...prev, { ...result, name: playerName, team: playerTeam }]
            })
          }
        }
        setSearchQuery('')
      }
    } catch (error) {
      console.error('搜索失败:', error)
    } finally {
      setIsSearching(false)
    }
  }

  const removeTeam = (index: number) => {
    setTeams((prev) => prev.filter((_, i) => i !== index))
  }

  const removePlayer = (name: string, team?: string) => {
    const targetKey = buildPlayerKey(name, team)
    setPlayers((prev) =>
      prev.filter((player) => {
        const playerName = normalizeText(player?.name)
        const playerTeam = normalizeText(player?.team) || undefined
        return buildPlayerKey(playerName, playerTeam) !== targetKey
      })
    )
  }

  const handleSubmit = () => {
    onSubmit({
      teams,
      players: mergedPlayersForSubmit,
      customContext: customContext.trim() || undefined,
    })
  }

  return (
    <div className="max-w-4xl mx-auto space-y-6">
      <div className="text-center">
        <h2 className="text-2xl font-bold">补充背景信息</h2>
        <p className="text-muted-foreground mt-2">
          添加相关战队和选手信息，帮助生成更准确的新闻稿
        </p>
      </div>

      {/* HLTV搜索 */}
      <div className="border rounded-lg p-4 space-y-4">
        <h3 className="font-semibold">从HLTV获取信息</h3>

        <div className="flex gap-2">
          <select
            value={searchType}
            onChange={(e) => setSearchType(e.target.value as 'team' | 'player')}
            className="border rounded px-3 py-2"
          >
            <option value="team">战队</option>
            <option value="player">选手</option>
          </select>
          <Input
            placeholder={`输入${searchType === 'team' ? '战队' : '选手'}名称...`}
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && handleSearch()}
            className="flex-1"
          />
          <Button onClick={handleSearch} disabled={isSearching || !searchQuery.trim()}>
            {isSearching ? <Loader2 className="h-4 w-4 animate-spin" /> : <Plus className="h-4 w-4" />}
          </Button>
        </div>

        {/* 已添加的战队 */}
        {teams.length > 0 && (
          <div className="space-y-2">
            <h4 className="text-sm font-medium">战队</h4>
            <div className="flex flex-wrap gap-2">
              {teams.map((team, index) => (
                <div
                  key={index}
                  className="flex items-center gap-2 bg-secondary px-3 py-1 rounded-full text-sm"
                >
                  <span>{team.name}</span>
                  {team.ranking && <span className="text-muted-foreground">#{team.ranking}</span>}
                  <button onClick={() => removeTeam(index)}>
                    <X className="h-3 w-3" />
                  </button>
                </div>
              ))}
            </div>
          </div>
        )}

        {/* 已添加的选手 */}
        {mergedPlayersForView.length > 0 && (
          <div className="space-y-2">
            <h4 className="text-sm font-medium">选手</h4>
            <div className="flex flex-wrap gap-2">
              {mergedPlayersForView.map((player) => (
                <div
                  key={`${player.name}-${player.team || ''}`}
                  className="flex items-center gap-2 bg-secondary px-3 py-1 rounded-full text-sm"
                >
                  <span>{player.name}</span>
                  {player.team && <span className="text-muted-foreground">({player.team})</span>}
                  {player.isManual ? (
                    <button onClick={() => removePlayer(player.name, player.team)}>
                      <X className="h-3 w-3" />
                    </button>
                  ) : (
                    <span className="text-muted-foreground">战队阵容</span>
                  )}
                </div>
              ))}
            </div>
          </div>
        )}

        {/* 已同步的教练 */}
        {coachTags.length > 0 && (
          <div className="space-y-2">
            <h4 className="text-sm font-medium">教练</h4>
            <div className="flex flex-wrap gap-2">
              {coachTags.map((item) => (
                <div
                  key={`${item.coach}-${item.team}`}
                  className="flex items-center gap-2 bg-secondary px-3 py-1 rounded-full text-sm"
                >
                  <span>{item.coach}</span>
                  <span className="text-muted-foreground">({item.team})</span>
                </div>
              ))}
            </div>
          </div>
        )}
      </div>

      {/* 自定义背景信息 */}
      <div className="border rounded-lg p-4 space-y-4">
        <h3 className="font-semibold">自定义背景信息</h3>
        <Textarea
          placeholder="输入任何额外的背景信息，如近期比赛结果、转会消息等..."
          value={customContext}
          onChange={(e) => setCustomContext(e.target.value)}
          rows={4}
        />
      </div>

      <div className="flex justify-end gap-2">
        <Button variant="outline" onClick={onSkip}>
          跳过
        </Button>
        <Button onClick={handleSubmit}>生成新闻稿</Button>
      </div>
    </div>
  )
}
