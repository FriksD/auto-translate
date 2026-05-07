import { useState } from 'react'
import { Button } from '@/components/ui/button'
import { Textarea } from '@/components/ui/textarea'
import { Download, RefreshCw, Copy, Check } from 'lucide-react'

interface ArticleEditorProps {
  article: string
  onChange: (article: string) => void
  onRegenerate: () => void
}

export function ArticleEditor({ article, onChange, onRegenerate }: ArticleEditorProps) {
  const [copied, setCopied] = useState(false)

  const handleCopy = async () => {
    await navigator.clipboard.writeText(article)
    setCopied(true)
    setTimeout(() => setCopied(false), 2000)
  }

  const handleExport = () => {
    const blob = new Blob([article], { type: 'text/markdown' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = 'article.md'
    a.click()
    URL.revokeObjectURL(url)
  }

  return (
    <div className="max-w-4xl mx-auto space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-2xl font-bold">新闻稿</h2>
          <p className="text-muted-foreground mt-1">
            编辑并导出最终的新闻稿
          </p>
        </div>
        <div className="flex gap-2">
          <Button variant="outline" size="sm" onClick={handleCopy}>
            {copied ? <Check className="h-4 w-4 mr-2" /> : <Copy className="h-4 w-4 mr-2" />}
            {copied ? '已复制' : '复制'}
          </Button>
          <Button variant="outline" size="sm" onClick={onRegenerate}>
            <RefreshCw className="h-4 w-4 mr-2" />
            重新生成
          </Button>
          <Button size="sm" onClick={handleExport}>
            <Download className="h-4 w-4 mr-2" />
            导出
          </Button>
        </div>
      </div>

      <Textarea
        value={article}
        onChange={(e) => onChange(e.target.value)}
        className="min-h-[600px] font-mono text-sm"
        placeholder="新闻稿内容将在这里显示..."
      />

      {!article && (
        <div className="text-center text-muted-foreground">
          点击"重新生成"按钮生成新闻稿
        </div>
      )}
    </div>
  )
}
