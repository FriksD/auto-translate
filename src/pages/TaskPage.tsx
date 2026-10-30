import { useCallback, useEffect, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { AlertTriangle, CheckCircle2, Download, Loader2, Play, Square, Trash2, XCircle } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Progress } from '@/components/ui/progress'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { Badge, Card, ErrorText, Select } from '@/components/ui/form'
import { ContextEditor } from '@/components/task/ContextEditor'
import { LivePanel } from '@/components/task/LivePanel'
import { MatchCard } from '@/components/task/MatchCard'
import { StageStepper } from '@/components/task/StageStepper'
import { TaskInfoEditor } from '@/components/task/TaskInfoEditor'
import { TextEditor } from '@/components/task/TextEditor'
import { api, formatSeconds, formatTime, isLive, isRunning, StageMeta, STATE_COLORS, STATE_LABELS, TaskDetail } from '@/lib/api'
import { cn } from '@/lib/utils'

const METHOD_LABELS: Record<string, string> = {
  exact: '拼写归一',
  alias: '术语表别名',
  phonetic: '发音推断',
  skeleton: '发音推断',
  llm: '上下文推断',
}

const LIVE_HINTS: Record<string, string> = {
  reserved: '已预约，开赛前 15 分钟开始自动监视比分。',
  watching: '监视中：决胜图接近结束时自动开始录制官方直播。',
  recording: '录制中：边录边转写，比赛结束后自动定位赛后采访。',
  interview_detecting: '比赛已结束，正在监听赛后采访；采访结束后自动截取并开始翻译。',
  no_interview: '未发现采访，录制已停止。如有采访，可在「直播」里手动标注起止，或在「任务信息」里上传音频。',
}

function NamesReportView({ task }: { task: TaskDetail }) {
  const report = task.names_report
  if (!report) return <p className="py-8 text-center text-sm text-muted-foreground">中文成稿后生成</p>
  return (
    <div className="space-y-5 text-sm">
      <section>
        <h3 className="mb-2 font-medium">待人工核对（推断不出来的）</h3>
        {report.suspects.length === 0 && report.ambiguous.length === 0 ? (
          <p className="text-muted-foreground">无</p>
        ) : (
          <ul className="space-y-1">
            {report.suspects.map((s) => (
              <li key={s.token} className="text-amber-700">
                {s.token} → 可能是 {s.candidates.join(' / ')}
              </li>
            ))}
            {report.ambiguous.map((s) => (
              <li key={s.token} className="text-amber-700">
                {s.token} → 多个规范名冲突：{s.candidates.join(' / ')}
              </li>
            ))}
          </ul>
        )}
      </section>
      <section>
        <h3 className="mb-2 font-medium">已自动替换</h3>
        <p className="mb-2 text-xs text-muted-foreground">标黄的是按发音或上下文推断出来的，建议精校时顺带确认。</p>
        {report.replaced.length === 0 ? (
          <p className="text-muted-foreground">无</p>
        ) : (
          <ul className="space-y-1">
            {report.replaced.map((r) => (
              <li key={`${r.from}-${r.to}`}>
                <span className="text-muted-foreground line-through">{r.from}</span> → {r.to}
                {r.count > 1 && <span className="text-muted-foreground"> ×{r.count}</span>}
                <span
                  className={cn(
                    'ml-2 rounded px-1.5 py-0.5 text-xs',
                    r.method === 'exact' || r.method === 'alias' ? 'bg-slate-100 text-slate-600' : 'bg-amber-100 text-amber-800',
                  )}
                >
                  {METHOD_LABELS[r.method] ?? r.method}
                </span>
              </li>
            ))}
          </ul>
        )}
      </section>
      <section>
        <h3 className="mb-2 font-medium">未收录的英文词</h3>
        <p className="text-muted-foreground">
          {report.unknown.length ? report.unknown.join('、') : '无'}
        </p>
        <p className="mt-1 text-xs text-muted-foreground">如果其中有专有名词，可以加到背景信息或术语表里，再从「中文成稿」重跑。</p>
      </section>
    </div>
  )
}

function TranscriptView({ task }: { task: TaskDetail }) {
  if (!task.transcript) return <p className="py-8 text-center text-sm text-muted-foreground">转写后生成</p>
  return (
    <div className="max-h-[520px] space-y-1 overflow-y-auto text-sm">
      <p className="mb-2 text-xs text-muted-foreground">语言：{task.transcript.language}</p>
      {task.transcript.segments.map((seg, i) => (
        <p key={i}>
          <span className="mr-2 font-mono text-xs text-muted-foreground">{formatSeconds(seg.start)}</span>
          {seg.speaker && <span className="mr-2 text-xs text-blue-700">{seg.speaker}</span>}
          {seg.text}
        </p>
      ))}
    </div>
  )
}

function EventLog({ task }: { task: TaskDetail }) {
  return (
    <div className="max-h-[520px] space-y-1 overflow-y-auto font-mono text-xs">
      {[...task.events].reverse().map((event) => (
        <p
          key={event.id}
          className={cn(event.level === 'error' && 'text-red-700', event.level === 'warning' && 'text-amber-700')}
        >
          <span className="mr-2 text-muted-foreground">{formatTime(event.ts)}</span>
          {event.message}
        </p>
      ))}
    </div>
  )
}

export function TaskPage() {
  const { id } = useParams()
  const taskId = Number(id)
  const navigate = useNavigate()
  const [task, setTask] = useState<TaskDetail | null>(null)
  const [stages, setStages] = useState<StageMeta[]>([])
  const [rerunFrom, setRerunFrom] = useState('context')
  const [error, setError] = useState('')

  const load = useCallback(async () => {
    try {
      setTask(await api.getTask(taskId))
      setError('')
    } catch (e) {
      setError((e as Error).message)
    }
  }, [taskId])

  useEffect(() => {
    api.meta().then((m) => setStages(m.stages)).catch(() => undefined)
  }, [])

  const running = task ? isRunning(task.state) : false
  useEffect(() => {
    load()
    const timer = setInterval(load, running ? 1500 : 5000)
    return () => clearInterval(timer)
  }, [load, running])

  // 失败时默认从失败的阶段重跑
  useEffect(() => {
    if (task?.state === 'failed' && task.stage) setRerunFrom(task.stage)
  }, [task?.state, task?.stage])

  const act = async (fn: () => Promise<unknown>) => {
    setError('')
    try {
      await fn()
      await load()
    } catch (e) {
      setError((e as Error).message)
    }
  }

  if (!task) {
    return error ? <ErrorText>{error}</ErrorText> : <Loader2 className="mx-auto mt-20 h-6 w-6 animate-spin" />
  }

  const progress = task.progress
  const saveText = (name: string) => async (text: string) => {
    await api.saveText(task.id, name, text)
    await load()
  }
  const saveAndRun = (name: string, fromStage: string) => async (text: string) => {
    await api.saveText(task.id, name, text)
    await api.runTask(task.id, fromStage)
    await load()
  }

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div className="space-y-1">
          <div className="flex items-center gap-3">
            <h1 className="text-xl font-semibold">{task.title}</h1>
            <Badge className={STATE_COLORS[task.state] ?? 'bg-slate-100'}>{STATE_LABELS[task.state] ?? task.state}</Badge>
          </div>
          <p className="text-sm text-muted-foreground">
            #{task.id} · 创建于 {formatTime(task.created_at)}
            {task.source_type === 'url' && task.source_url && <> · {task.source_url}</>}
            {(task.clip_start !== null || task.clip_end !== null) && (
              <> · 截取 {formatSeconds(task.clip_start) || '开头'} ~ {formatSeconds(task.clip_end) || '结尾'}</>
            )}
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          {task.docx && (
            <a href={api.fileUrl(task.id, task.docx)}>
              <Button>
                <Download className="mr-2 h-4 w-4" />
                下载 Word
              </Button>
            </a>
          )}
          {task.state === 'review' && (
            <Button variant="outline" onClick={() => act(() => api.markDone(task.id))}>
              <CheckCircle2 className="mr-2 h-4 w-4" />
              标记完成
            </Button>
          )}
          {isLive(task.state) && (
            <Button
              variant="outline"
              onClick={() => {
                if (confirm('确定取消这场比赛？会停止监视和录制。')) act(() => api.cancelTask(task.id))
              }}
            >
              <XCircle className="mr-2 h-4 w-4" />
              {task.state === 'reserved' || task.state === 'watching' ? '取消预约' : '停止并取消'}
            </Button>
          )}
          {running ? (
            <Button variant="outline" onClick={() => act(() => api.cancelTask(task.id))}>
              <Square className="mr-2 h-4 w-4" />
              取消
            </Button>
          ) : (
            <>
              <Select className="w-40" value={rerunFrom} onChange={(e) => setRerunFrom(e.target.value)}>
                {stages.map((s) => (
                  <option key={s.key} value={s.key}>
                    从「{s.label}」
                  </option>
                ))}
              </Select>
              <Button variant="outline" onClick={() => act(() => api.runTask(task.id, rerunFrom))}>
                <Play className="mr-2 h-4 w-4" />
                {task.state === 'draft' ? '开始' : '重跑'}
              </Button>
              <Button
                variant="ghost"
                size="icon"
                title="删除任务"
                onClick={() => {
                  if (confirm('确定删除这个任务及其所有文件？')) act(async () => {
                    await api.deleteTask(task.id)
                    navigate('/')
                  })
                }}
              >
                <Trash2 className="h-4 w-4" />
              </Button>
            </>
          )}
        </div>
      </div>

      <MatchCard context={task.context} />

      <Card className="space-y-3">
        <StageStepper task={task} stages={stages} />
        {running && progress && (
          <div className="space-y-1">
            {progress.percent !== null && <Progress value={progress.percent} className="h-2" />}
            <p className="text-sm text-muted-foreground">{progress.message}</p>
          </div>
        )}
        {task.state === 'failed' && task.error && (
          <p className="flex items-center gap-2 text-sm text-red-700">
            <AlertTriangle className="h-4 w-4" />
            {task.error}
          </p>
        )}
        {LIVE_HINTS[task.state] && task.source_type === 'live' && (
          <p className="text-sm text-violet-700">{LIVE_HINTS[task.state]}</p>
        )}
        {task.state === 'draft' && task.source_type === 'file' && !task.source_file && (
          <p className="text-sm text-amber-700">尚未上传音频，请在「任务信息」里上传。</p>
        )}
        {task.names_report && task.names_report.suspects.length > 0 && (
          <p className="text-sm text-amber-700">
            有 {task.names_report.suspects.length} 个名词需要人工核对，见「名词报告」。
          </p>
        )}
      </Card>

      <ErrorText>{error}</ErrorText>

      <Card>
        <Tabs defaultValue={task.source_type === 'live' && isLive(task.state) ? 'live' : 'draft'}>
          <TabsList className="mb-4 flex-wrap">
            {task.source_type === 'live' && <TabsTrigger value="live">直播</TabsTrigger>}
            <TabsTrigger value="draft">中文稿</TabsTrigger>
            <TabsTrigger value="clean">英文清洗稿</TabsTrigger>
            <TabsTrigger value="transcript">转写原文</TabsTrigger>
            <TabsTrigger value="names">名词报告</TabsTrigger>
            <TabsTrigger value="context">背景信息</TabsTrigger>
            <TabsTrigger value="info">任务信息</TabsTrigger>
            <TabsTrigger value="log">日志</TabsTrigger>
          </TabsList>
          {task.source_type === 'live' && (
            <TabsContent value="live">
              <LivePanel task={task} onChanged={load} />
            </TabsContent>
          )}
          <TabsContent value="draft">
            <TextEditor
              value={task.texts['draft_zh.txt']}
              emptyText="中文成稿后生成"
              disabled={running}
              onSave={saveText('draft_zh.txt')}
              rerunLabel="保存并重新生成 Word"
              onSaveAndRerun={saveAndRun('draft_zh.txt', 'export')}
            />
          </TabsContent>
          <TabsContent value="clean">
            <TextEditor
              value={task.texts['clean_en.txt']}
              emptyText="英文清洗后生成"
              disabled={running}
              onSave={saveText('clean_en.txt')}
              rerunLabel="保存并重新翻译"
              onSaveAndRerun={saveAndRun('clean_en.txt', 'write')}
            />
          </TabsContent>
          <TabsContent value="transcript">
            <TranscriptView task={task} />
          </TabsContent>
          <TabsContent value="names">
            <NamesReportView task={task} />
          </TabsContent>
          <TabsContent value="context">
            <ContextEditor
              context={task.context}
              disabled={running}
              onSave={async (context) => {
                await api.updateTask(task.id, { context })
                await load()
              }}
              onRefetch={() =>
                act(async () => {
                  await api.updateTask(task.id, { context: { ...task.context, teams: [], match: undefined } })
                  await api.runTask(task.id, 'context', 'context')
                })
              }
            />
          </TabsContent>
          <TabsContent value="info">
            <TaskInfoEditor
              task={task}
              disabled={running}
              onSave={async (patch) => {
                await api.updateTask(task.id, patch)
                await load()
              }}
              onUpload={async (file) => {
                await api.uploadSource(task.id, file)
                await load()
              }}
            />
          </TabsContent>
          <TabsContent value="log">
            <EventLog task={task} />
          </TabsContent>
        </Tabs>
      </Card>
    </div>
  )
}
