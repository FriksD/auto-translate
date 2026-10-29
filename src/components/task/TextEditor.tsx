import { useEffect, useState } from 'react'
import { Loader2, Save } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Textarea } from '@/components/ui/textarea'
import { ErrorText } from '@/components/ui/form'

interface Props {
  value: string | null
  emptyText: string
  disabled?: boolean
  onSave: (text: string) => Promise<void>
  // 保存后从指定阶段重跑，例如改中文稿后重新生成 Word
  rerunLabel?: string
  onSaveAndRerun?: (text: string) => Promise<void>
}

export function TextEditor({ value, emptyText, disabled, onSave, rerunLabel, onSaveAndRerun }: Props) {
  const [text, setText] = useState(value ?? '')
  const [dirty, setDirty] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  // 服务端内容变化时同步，但不覆盖未保存的修改
  useEffect(() => {
    if (!dirty) setText(value ?? '')
  }, [value, dirty])

  if (value === null && !dirty) {
    return <p className="py-8 text-center text-sm text-muted-foreground">{emptyText}</p>
  }

  const run = async (fn: (text: string) => Promise<void>) => {
    setBusy(true)
    setError('')
    try {
      await fn(text)
      setDirty(false)
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="space-y-3">
      <Textarea
        className="min-h-[420px] font-mono text-sm leading-relaxed"
        value={text}
        disabled={disabled}
        onChange={(e) => {
          setText(e.target.value)
          setDirty(true)
        }}
      />
      <ErrorText>{error}</ErrorText>
      <div className="flex items-center gap-2">
        <Button size="sm" variant="outline" disabled={!dirty || busy || disabled} onClick={() => run(onSave)}>
          {busy ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : <Save className="mr-2 h-4 w-4" />}
          保存
        </Button>
        {onSaveAndRerun && (
          <Button size="sm" disabled={busy || disabled} onClick={() => run(onSaveAndRerun)}>
            {rerunLabel}
          </Button>
        )}
        {dirty && <span className="text-xs text-amber-600">有未保存的修改</span>}
      </div>
    </div>
  )
}
