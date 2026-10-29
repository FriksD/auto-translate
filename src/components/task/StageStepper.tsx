import { Check, CircleDashed, Loader2, X } from 'lucide-react'
import { StageMeta, TaskDetail } from '@/lib/api'
import { cn } from '@/lib/utils'

type StageStatus = 'done' | 'active' | 'failed' | 'pending'

const ICONS = { done: Check, active: Loader2, failed: X, pending: CircleDashed }

const STYLES: Record<StageStatus, string> = {
  done: 'border-emerald-200 bg-emerald-50 text-emerald-700',
  active: 'border-blue-300 bg-blue-50 text-blue-700',
  failed: 'border-red-300 bg-red-50 text-red-700',
  pending: 'border-slate-200 text-slate-400',
}

export function StageStepper({ task, stages }: { task: TaskDetail; stages: StageMeta[] }) {
  const currentIndex = stages.findIndex((s) => s.key === task.stage)
  const finished = task.state === 'review' || task.state === 'done'

  return (
    <ol className="flex flex-wrap items-center gap-2">
      {stages.map((stage, index) => {
        let status: StageStatus = 'pending'
        // 已预约的任务只跑过「抓取背景信息」
        const contextReady = stage.key === 'context' && !task.stage && (task.context.teams?.length ?? 0) > 0
        if (finished || contextReady || (currentIndex >= 0 && index < currentIndex)) status = 'done'
        else if (index === currentIndex && (task.state === 'processing' || task.state === 'queued')) status = 'active'
        else if (index === currentIndex && task.state === 'failed') status = 'failed'
        const Icon = ICONS[status]
        return (
          <li
            key={stage.key}
            className={cn('inline-flex items-center gap-1.5 rounded-full border px-3 py-1 text-xs', STYLES[status])}
          >
            <Icon className={cn('h-3.5 w-3.5', status === 'active' && 'animate-spin')} />
            {stage.label}
          </li>
        )
      })}
    </ol>
  )
}
