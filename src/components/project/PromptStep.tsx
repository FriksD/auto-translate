import { Button } from '@/components/ui/button'
import { Textarea } from '@/components/ui/textarea'
import { Loader2 } from 'lucide-react'

interface PromptStepProps {
  prompt: string
  isLoading: boolean
  isGenerating: boolean
  error: string | null
  onPromptChange: (value: string) => void
  onBack: () => void
  onRetry: () => void
  onGenerate: () => void
}

export function PromptStep({
  prompt,
  isLoading,
  isGenerating,
  error,
  onPromptChange,
  onBack,
  onRetry,
  onGenerate,
}: PromptStepProps) {
  return (
    <div className="max-w-4xl mx-auto space-y-6">
      <div className="text-center">
        <h2 className="text-2xl font-bold">确认提示词</h2>
        <p className="text-muted-foreground mt-2">
          下方内容将发送给 AI，你可以在生成新闻稿前先检查或微调
        </p>
      </div>

      {isLoading ? (
        <div className="border rounded-lg p-8 text-center text-muted-foreground">
          <Loader2 className="h-5 w-5 animate-spin mx-auto mb-3" />
          正在生成提示词...
        </div>
      ) : (
        <div className="space-y-3">
          <Textarea
            value={prompt}
            onChange={(event) => onPromptChange(event.target.value)}
            className="min-h-[520px] font-mono text-xs leading-5"
            placeholder="提示词将在这里显示..."
          />
          {error && <p className="text-sm text-red-500">{error}</p>}
        </div>
      )}

      <div className="flex justify-end gap-2">
        <Button variant="outline" onClick={onBack} disabled={isLoading || isGenerating}>
          返回补充信息
        </Button>
        {error && (
          <Button variant="outline" onClick={onRetry} disabled={isLoading || isGenerating}>
            重新获取提示词
          </Button>
        )}
        <Button onClick={onGenerate} disabled={isLoading || isGenerating || !prompt.trim()}>
          {isGenerating ? <Loader2 className="h-4 w-4 mr-2 animate-spin" /> : null}
          生成新闻稿
        </Button>
      </div>
    </div>
  )
}
