import { FormEvent, useCallback, useEffect, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { Loader2, Plus } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Textarea } from '@/components/ui/textarea'
import { Badge, Card, CardTitle, ErrorText, Field, Select } from '@/components/ui/form'
import { api, formatMatchTime, formatTime, isRunning, parseSeconds, STATE_COLORS, STATE_LABELS, Task } from '@/lib/api'

function NewTaskForm({ onCreated }: { onCreated: (task: Task) => void }) {
  const [title, setTitle] = useState('')
  const [matchUrl, setMatchUrl] = useState('')
  const [sourceType, setSourceType] = useState<'url' | 'file'>('url')
  const [url, setUrl] = useState('')
  const [file, setFile] = useState<File | null>(null)
  const [team1, setTeam1] = useState('')
  const [team2, setTeam2] = useState('')
  const [clipStart, setClipStart] = useState('')
  const [clipEnd, setClipEnd] = useState('')
  const [notes, setNotes] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState('')

  const submit = async (event: FormEvent) => {
    event.preventDefault()
    setError('')
    if (sourceType === 'file' && !file) {
      setError('请选择音频或视频文件')
      return
    }
    setSubmitting(true)
    try {
      const task = await api.createTask({
        title: title.trim() || [team1, team2].filter(Boolean).join(' vs ') || '未命名采访',
        source_type: sourceType,
        source_url: sourceType === 'url' ? url.trim() : null,
        team1: team1.trim() || null,
        team2: team2.trim() || null,
        clip_start: parseSeconds(clipStart),
        clip_end: parseSeconds(clipEnd),
        notes: notes.trim() || null,
        match_url: matchUrl.trim() || null,
      })
      if (sourceType === 'file' && file) {
        await api.uploadSource(task.id, file)
      }
      onCreated(task)
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <Card>
      <CardTitle>新建任务</CardTitle>
      <form onSubmit={submit} className="space-y-4">
        <div className="grid gap-4 md:grid-cols-3">
          <Field label="标题" hint="留空则用「队伍1 vs 队伍2」">
            <Input value={title} onChange={(e) => setTitle(e.target.value)} placeholder="例如：Falcons vs Vitality 赛后采访" />
          </Field>
          <Field label="队伍 1" hint="用于从 HLTV 抓取阵容">
            <Input value={team1} onChange={(e) => setTeam1(e.target.value)} placeholder="Falcons" />
          </Field>
          <Field label="队伍 2">
            <Input value={team2} onChange={(e) => setTeam2(e.target.value)} placeholder="Vitality" />
          </Field>
        </div>

        <Field label="HLTV 比赛链接" hint="可选。填了会按这场比赛抓取当时的阵容、比分、BP 等，队伍可以不填">
          <Input value={matchUrl} onChange={(e) => setMatchUrl(e.target.value)} placeholder="https://www.hltv.org/matches/2381773/..." />
        </Field>

        <div className="grid gap-4 md:grid-cols-[160px_1fr]">
          <Field label="音频来源">
            <Select value={sourceType} onChange={(e) => setSourceType(e.target.value as 'url' | 'file')}>
              <option value="url">视频链接</option>
              <option value="file">本地文件</option>
            </Select>
          </Field>
          {sourceType === 'url' ? (
            <Field label="链接" hint="YouTube / Twitch / B站等 yt-dlp 支持的地址，走设置里的代理">
              <Input value={url} onChange={(e) => setUrl(e.target.value)} placeholder="https://www.youtube.com/watch?v=..." />
            </Field>
          ) : (
            <Field label="文件" hint="音频或视频文件均可">
              <Input type="file" accept="audio/*,video/*" onChange={(e) => setFile(e.target.files?.[0] ?? null)} />
            </Field>
          )}
        </div>

        <div className="grid gap-4 md:grid-cols-3">
          <Field label="截取开始" hint="可选，mm:ss 或 hh:mm:ss">
            <Input value={clipStart} onChange={(e) => setClipStart(e.target.value)} placeholder="例如 12:30" />
          </Field>
          <Field label="截取结束" hint="可选，留空到结尾">
            <Input value={clipEnd} onChange={(e) => setClipEnd(e.target.value)} placeholder="例如 17:45" />
          </Field>
          <Field label="本稿要求" hint="可选，会传给 LLM">
            <Textarea className="min-h-10" rows={1} value={notes} onChange={(e) => setNotes(e.target.value)} />
          </Field>
        </div>

        <ErrorText>{error}</ErrorText>
        <Button type="submit" disabled={submitting}>
          {submitting ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : <Plus className="mr-2 h-4 w-4" />}
          创建并开始
        </Button>
      </form>
    </Card>
  )
}

export function TasksPage() {
  const navigate = useNavigate()
  const [tasks, setTasks] = useState<Task[]>([])
  const [error, setError] = useState('')
  const [showForm, setShowForm] = useState(false)

  const load = useCallback(async () => {
    try {
      setTasks(await api.listTasks())
      setError('')
    } catch (e) {
      setError(`无法连接后端：${(e as Error).message}`)
    }
  }, [])

  useEffect(() => {
    load()
    const timer = setInterval(load, 3000)
    return () => clearInterval(timer)
  }, [load])

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-semibold">任务</h1>
        <Button variant={showForm ? 'outline' : 'default'} onClick={() => setShowForm((v) => !v)}>
          {showForm ? '收起' : (<><Plus className="mr-2 h-4 w-4" />新建任务</>)}
        </Button>
      </div>

      {showForm && <NewTaskForm onCreated={(task) => navigate(`/tasks/${task.id}`)} />}
      <ErrorText>{error}</ErrorText>

      <Card className="p-0">
        {tasks.length === 0 ? (
          <p className="p-8 text-center text-sm text-muted-foreground">还没有任务</p>
        ) : (
          <table className="w-full text-sm">
            <thead className="border-b text-left text-muted-foreground">
              <tr>
                <th className="px-5 py-3 font-medium">#</th>
                <th className="px-5 py-3 font-medium">标题</th>
                <th className="px-5 py-3 font-medium">状态</th>
                <th className="px-5 py-3 font-medium">比赛时间</th>
                <th className="px-5 py-3 font-medium">进度</th>
                <th className="px-5 py-3 font-medium">创建时间</th>
              </tr>
            </thead>
            <tbody>
              {tasks.map((task) => (
                <tr key={task.id} className="border-b last:border-0 hover:bg-slate-50">
                  <td className="px-5 py-3 text-muted-foreground">{task.id}</td>
                  <td className="px-5 py-3">
                    <Link to={`/tasks/${task.id}`} className="font-medium hover:underline">
                      {task.title}
                    </Link>
                  </td>
                  <td className="px-5 py-3">
                    <Badge className={STATE_COLORS[task.state] ?? 'bg-slate-100'}>
                      {STATE_LABELS[task.state] ?? task.state}
                    </Badge>
                  </td>
                  <td className="whitespace-nowrap px-5 py-3 text-muted-foreground">
                    {task.match_time ? formatMatchTime(task.match_time * 1000) : ''}
                  </td>
                  <td className="max-w-xs truncate px-5 py-3 text-muted-foreground">
                    {isRunning(task.state) ? task.progress?.message : task.state === 'failed' ? task.error : ''}
                  </td>
                  <td className="px-5 py-3 text-muted-foreground">{formatTime(task.created_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Card>
    </div>
  )
}
