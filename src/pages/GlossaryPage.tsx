import { FormEvent, useEffect, useState } from 'react'
import { Plus, Trash2 } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Card, CardTitle, ErrorText, Field, Select } from '@/components/ui/form'
import { api, GlossaryEntry } from '@/lib/api'

const KIND_LABELS = { alias: '别名 → 规范名', term: '术语 → 中文译法' }

export function GlossaryPage() {
  const [entries, setEntries] = useState<GlossaryEntry[]>([])
  const [form, setForm] = useState<GlossaryEntry>({ kind: 'alias', term: '', target: '', note: '' })
  const [error, setError] = useState('')

  const load = () => api.listGlossary().then(setEntries).catch((e) => setError(e.message))
  useEffect(() => {
    load()
  }, [])

  const submit = async (event: FormEvent) => {
    event.preventDefault()
    setError('')
    try {
      await api.saveGlossary(form)
      setForm({ ...form, term: '', target: '', note: '' })
      await load()
    } catch (e) {
      setError((e as Error).message)
    }
  }

  const remove = async (id: number) => {
    await api.deleteGlossary(id)
    await load()
  }

  return (
    <div className="space-y-6">
      <h1 className="text-xl font-semibold">术语表</h1>
      <Card>
        <CardTitle>添加词条</CardTitle>
        <p className="mb-4 text-sm text-muted-foreground">
          <strong>别名</strong>：把稿件里出现的写法统一成 HLTV 规范名，如 NAVI → Natus Vincere、小蜜蜂 → Vitality，会在名词规范化时自动替换。
          <br />
          <strong>术语</strong>：英文术语的固定中文译法，如 eco → 经济局，会写进翻译提示词。
        </p>
        <form onSubmit={submit} className="grid items-end gap-3 md:grid-cols-[180px_1fr_1fr_1fr_auto]">
          <Field label="类型">
            <Select value={form.kind} onChange={(e) => setForm({ ...form, kind: e.target.value as GlossaryEntry['kind'] })}>
              <option value="alias">{KIND_LABELS.alias}</option>
              <option value="term">{KIND_LABELS.term}</option>
            </Select>
          </Field>
          <Field label={form.kind === 'alias' ? '别名' : '英文术语'}>
            <Input value={form.term} onChange={(e) => setForm({ ...form, term: e.target.value })} />
          </Field>
          <Field label={form.kind === 'alias' ? 'HLTV 规范名' : '中文译法'}>
            <Input value={form.target} onChange={(e) => setForm({ ...form, target: e.target.value })} />
          </Field>
          <Field label="备注">
            <Input value={form.note ?? ''} onChange={(e) => setForm({ ...form, note: e.target.value })} />
          </Field>
          <Button type="submit">
            <Plus className="mr-2 h-4 w-4" />
            添加
          </Button>
        </form>
        <div className="mt-3">
          <ErrorText>{error}</ErrorText>
        </div>
      </Card>

      <Card className="p-0">
        {entries.length === 0 ? (
          <p className="p-8 text-center text-sm text-muted-foreground">还没有词条</p>
        ) : (
          <table className="w-full text-sm">
            <thead className="border-b text-left text-muted-foreground">
              <tr>
                <th className="px-5 py-3 font-medium">类型</th>
                <th className="px-5 py-3 font-medium">词条</th>
                <th className="px-5 py-3 font-medium">目标</th>
                <th className="px-5 py-3 font-medium">备注</th>
                <th className="px-5 py-3" />
              </tr>
            </thead>
            <tbody>
              {entries.map((entry) => (
                <tr key={entry.id} className="border-b last:border-0">
                  <td className="px-5 py-2 text-muted-foreground">{KIND_LABELS[entry.kind]}</td>
                  <td className="px-5 py-2">{entry.term}</td>
                  <td className="px-5 py-2 font-medium">{entry.target}</td>
                  <td className="px-5 py-2 text-muted-foreground">{entry.note}</td>
                  <td className="px-5 py-2 text-right">
                    <Button variant="ghost" size="icon" onClick={() => entry.id && remove(entry.id)}>
                      <Trash2 className="h-4 w-4" />
                    </Button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Card>
    </div>
  )
}
