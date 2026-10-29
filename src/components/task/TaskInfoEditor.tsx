import { useEffect, useState } from 'react'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Textarea } from '@/components/ui/textarea'
import { ErrorText, Field } from '@/components/ui/form'
import { formatSeconds, parseSeconds, Task, TaskDetail } from '@/lib/api'

interface Props {
  task: TaskDetail
  disabled: boolean
  onSave: (patch: Partial<Task>) => Promise<void>
  onUpload: (file: File) => Promise<void>
}

export function TaskInfoEditor({ task, disabled, onSave, onUpload }: Props) {
  const initial = () => ({
    title: task.title,
    source_url: task.source_url ?? '',
    team1: task.team1 ?? '',
    team2: task.team2 ?? '',
    clip_start: formatSeconds(task.clip_start),
    clip_end: formatSeconds(task.clip_end),
    notes: task.notes ?? '',
  })
  const [form, setForm] = useState(initial)
  const [dirty, setDirty] = useState(false)
  const [error, setError] = useState('')

  useEffect(() => {
    if (!dirty) setForm(initial())
  }, [task.updated_at, dirty])

  const set = (key: keyof ReturnType<typeof initial>) => (value: string) => {
    setForm((f) => ({ ...f, [key]: value }))
    setDirty(true)
  }

  const save = async () => {
    setError('')
    try {
      await onSave({
        title: form.title.trim() || task.title,
        source_url: form.source_url.trim() || null,
        team1: form.team1.trim() || null,
        team2: form.team2.trim() || null,
        clip_start: parseSeconds(form.clip_start),
        clip_end: parseSeconds(form.clip_end),
        notes: form.notes.trim() || null,
      })
      setDirty(false)
    } catch (e) {
      setError((e as Error).message)
    }
  }

  return (
    <div className="space-y-4">
      <div className="grid gap-4 md:grid-cols-3">
        <Field label="标题">
          <Input value={form.title} disabled={disabled} onChange={(e) => set('title')(e.target.value)} />
        </Field>
        <Field label="队伍 1">
          <Input value={form.team1} disabled={disabled} onChange={(e) => set('team1')(e.target.value)} />
        </Field>
        <Field label="队伍 2">
          <Input value={form.team2} disabled={disabled} onChange={(e) => set('team2')(e.target.value)} />
        </Field>
      </div>
      {task.source_type === 'url' ? (
        <Field label="视频链接">
          <Input value={form.source_url} disabled={disabled} onChange={(e) => set('source_url')(e.target.value)} />
        </Field>
      ) : (
        <Field
          label={task.source_type === 'live' ? '上传采访音频' : '重新上传文件'}
          hint="上传后会自动开始处理（已抓取的背景信息会保留）"
        >
          <Input
            type="file"
            accept="audio/*,video/*"
            disabled={disabled}
            onChange={async (e) => {
              const file = e.target.files?.[0]
              if (!file) return
              try {
                await onUpload(file)
              } catch (err) {
                setError((err as Error).message)
              }
            }}
          />
        </Field>
      )}
      <div className="grid gap-4 md:grid-cols-2">
        <Field label="截取开始" hint="mm:ss，修改后需从「获取音频」重跑">
          <Input value={form.clip_start} disabled={disabled} onChange={(e) => set('clip_start')(e.target.value)} />
        </Field>
        <Field label="截取结束">
          <Input value={form.clip_end} disabled={disabled} onChange={(e) => set('clip_end')(e.target.value)} />
        </Field>
      </div>
      <Field label="本稿要求" hint="修改后需从「中文成稿」重跑">
        <Textarea value={form.notes} disabled={disabled} onChange={(e) => set('notes')(e.target.value)} />
      </Field>
      <ErrorText>{error}</ErrorText>
      <Button size="sm" disabled={!dirty || disabled} onClick={save}>
        保存
      </Button>
    </div>
  )
}
