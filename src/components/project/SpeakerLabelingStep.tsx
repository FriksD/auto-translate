import { useState, useMemo } from 'react'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { TranscriptSegment } from '@/stores/appStore'

interface SpeakerLabelingStepProps {
  transcript: TranscriptSegment[]
  initialSpeakers?: Record<string, string>
  onSubmit: (speakers: Record<string, string>) => void
}

export function SpeakerLabelingStep({
  transcript,
  initialSpeakers = {},
  onSubmit,
}: SpeakerLabelingStepProps) {
  const [speakers, setSpeakers] = useState<Record<string, string>>(initialSpeakers)

  // 获取所有唯一的说话人
  const uniqueSpeakers = useMemo(() => {
    const speakerSet = new Set<string>()
    transcript.forEach((seg) => {
      if (seg.speaker) {
        speakerSet.add(seg.speaker)
      }
    })
    return Array.from(speakerSet).sort()
  }, [transcript])

  const handleSpeakerNameChange = (speakerId: string, name: string) => {
    setSpeakers((prev) => ({
      ...prev,
      [speakerId]: name,
    }))
  }

  const handleSubmit = () => {
    onSubmit(speakers)
  }

  // 按说话人分组转录内容
  const groupedTranscript = useMemo(() => {
    const groups: Record<string, TranscriptSegment[]> = {}
    uniqueSpeakers.forEach((speaker) => {
      groups[speaker] = transcript.filter((seg) => seg.speaker === speaker)
    })
    return groups
  }, [transcript, uniqueSpeakers])

  return (
    <div className="max-w-4xl mx-auto space-y-6">
      <div className="text-center">
        <h2 className="text-2xl font-bold">标注说话人</h2>
        <p className="text-muted-foreground mt-2">
          为每个说话人输入名字，帮助我们生成更准确的新闻稿
        </p>
      </div>

      {/* 说话人输入 */}
      <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-3">
        {uniqueSpeakers.map((speaker) => (
          <div key={speaker} className="border rounded-lg p-4">
            <div className="flex items-center gap-2 mb-2">
              <div className="w-3 h-3 rounded-full bg-primary" />
              <span className="font-medium">{speaker}</span>
            </div>
            <Input
              placeholder="输入名字..."
              value={speakers[speaker] || ''}
              onChange={(e) => handleSpeakerNameChange(speaker, e.target.value)}
            />
            <div className="mt-2 text-sm text-muted-foreground">
              {groupedTranscript[speaker]?.length || 0} 个片段
            </div>
          </div>
        ))}
      </div>

      {/* 转录预览 */}
      <div className="border rounded-lg p-4 max-h-96 overflow-y-auto">
        <h3 className="font-semibold mb-4">转录预览</h3>
        <div className="space-y-2">
          {transcript.slice(0, 50).map((segment, index) => (
            <div key={index} className="flex gap-2 text-sm">
              <span className="font-medium text-primary min-w-[100px]">
                {speakers[segment.speaker || ''] || segment.speaker}:
              </span>
              <span className="text-muted-foreground">{segment.text}</span>
            </div>
          ))}
          {transcript.length > 50 && (
            <div className="text-muted-foreground text-sm">
              ... 还有 {transcript.length - 50} 个片段
            </div>
          )}
        </div>
      </div>

      <div className="flex justify-end gap-2">
        <Button variant="outline" onClick={() => onSubmit(speakers)}>
          跳过
        </Button>
        <Button onClick={handleSubmit}>继续</Button>
      </div>
    </div>
  )
}
