import { useEffect, useState } from 'react'
import { Plus, Trash2 } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Textarea } from '@/components/ui/textarea'
import { ErrorText, Field } from '@/components/ui/form'
import { TaskContext, TeamContext } from '@/lib/api'

const splitNames = (text: string) =>
  text
    .split(/[,，、\n]/)
    .map((s) => s.trim())
    .filter(Boolean)

interface TeamDraft {
  original: TeamContext
  name: string
  players: string
  coach: string
}

interface Draft {
  teams: TeamDraft[]
  extraNames: string
  background: string
}

function toDraft(context: TaskContext): Draft {
  return {
    teams: (context.teams ?? []).map((team) => ({
      original: team,
      name: team.name ?? '',
      players: (team.players ?? []).join(', '),
      coach: team.stats?.coach ?? team.coach ?? '',
    })),
    extraNames: (context.extra_names ?? []).join(', '),
    background: context.background ?? '',
  }
}

function fromDraft(draft: Draft, base: TaskContext): TaskContext {
  return {
    ...base,
    teams: draft.teams
      .filter((t) => t.name.trim())
      .map((t) => ({
        ...t.original,
        name: t.name.trim(),
        players: splitNames(t.players),
        stats: { ...(t.original.stats ?? {}), coach: t.coach.trim() || null },
      })),
    extra_names: splitNames(draft.extraNames),
    background: draft.background.trim(),
  }
}

interface Props {
  context: TaskContext
  disabled: boolean
  onSave: (context: TaskContext) => Promise<void>
  onRefetch: () => Promise<void>
}

export function ContextEditor({ context, disabled, onSave, onRefetch }: Props) {
  const [draft, setDraft] = useState<Draft>(() => toDraft(context))
  const [dirty, setDirty] = useState(false)
  const [error, setError] = useState('')

  useEffect(() => {
    if (!dirty) setDraft(toDraft(context))
  }, [context, dirty])

  const update = (patch: Partial<Draft>) => {
    setDraft((d) => ({ ...d, ...patch }))
    setDirty(true)
  }
  const updateTeam = (index: number, patch: Partial<TeamDraft>) =>
    update({ teams: draft.teams.map((t, i) => (i === index ? { ...t, ...patch } : t)) })

  const save = async () => {
    setError('')
    try {
      await onSave(fromDraft(draft, context))
      setDirty(false)
    } catch (e) {
      setError((e as Error).message)
    }
  }

  return (
    <div className="space-y-5">
      <p className="text-sm text-muted-foreground">
        这里的队名、选手名、教练名是稿件的<strong>规范写法</strong>：转写提示、LLM 名单和最终的名词替换都以此为准，必须与 HLTV 一致。
      </p>

      {draft.teams.map((team, index) => (
        <div key={index} className="space-y-3 rounded-md border p-4">
          {team.original.error && <ErrorText>抓取失败：{team.original.error}</ErrorText>}
          <div className="grid gap-3 md:grid-cols-[1fr_1fr_auto]">
            <Field label="战队名">
              <Input value={team.name} disabled={disabled} onChange={(e) => updateTeam(index, { name: e.target.value })} />
            </Field>
            <Field label="教练">
              <Input value={team.coach} disabled={disabled} onChange={(e) => updateTeam(index, { coach: e.target.value })} />
            </Field>
            <div className="flex items-end">
              <Button
                variant="ghost"
                size="icon"
                disabled={disabled}
                onClick={() => update({ teams: draft.teams.filter((_, i) => i !== index) })}
              >
                <Trash2 className="h-4 w-4" />
              </Button>
            </div>
          </div>
          <Field label="阵容" hint="逗号分隔">
            <Input value={team.players} disabled={disabled} onChange={(e) => updateTeam(index, { players: e.target.value })} />
          </Field>
          {team.original.recent_matches && team.original.recent_matches.length > 0 && (
            <div className="text-xs text-muted-foreground">
              近期比赛：
              {team.original.recent_matches
                .map((m) => `${m.team1} ${m.score} ${m.team2}（${m.event}）`)
                .join('；')}
            </div>
          )}
        </div>
      ))}

      <Button
        variant="outline"
        size="sm"
        disabled={disabled}
        onClick={() => update({ teams: [...draft.teams, { original: { name: '' }, name: '', players: '', coach: '' }] })}
      >
        <Plus className="mr-2 h-4 w-4" />
        添加战队
      </Button>

      <Field label="其他规范名" hint="主持人、其他选手、赛事名等，逗号分隔">
        <Input value={draft.extraNames} disabled={disabled} onChange={(e) => update({ extraNames: e.target.value })} />
      </Field>
      <Field label="补充背景" hint="比分、赛事阶段等，供 LLM 理解语境，不会直接写进稿件">
        <Textarea value={draft.background} disabled={disabled} onChange={(e) => update({ background: e.target.value })} />
      </Field>

      <ErrorText>{error}</ErrorText>
      <div className="flex gap-2">
        <Button size="sm" disabled={!dirty || disabled} onClick={save}>
          保存
        </Button>
        <Button size="sm" variant="outline" disabled={disabled} onClick={onRefetch}>
          清空并从 HLTV 重新抓取
        </Button>
      </div>
    </div>
  )
}
