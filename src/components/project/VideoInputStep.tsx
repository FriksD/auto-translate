import { useMemo, useState } from 'react'
import { Loader2, Upload } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import type { VideoInfo } from '@/stores/appStore'

type MediaSourceType = 'youtube' | 'local'

export interface MediaSubmitPayload {
  sourceType: MediaSourceType
  source: string
  speakerCount?: number
  previewInfo?: VideoInfo
}

interface VideoInputStepProps {
  initialInput?: string
  initialSpeakerCount?: number
  onSubmit: (payload: MediaSubmitPayload) => void
}

const mediaFileFilters = [
  {
    name: 'Media Files',
    extensions: [
      'mp4',
      'mkv',
      'mov',
      'avi',
      'webm',
      'flv',
      'mp3',
      'wav',
      'm4a',
      'aac',
      'flac',
      'ogg',
      'opus',
      'wma',
    ],
  },
]

function guessInitialSourceType(value: string): MediaSourceType {
  if (!value) {
    return 'youtube'
  }
  return /^https?:\/\//i.test(value) ? 'youtube' : 'local'
}

function getFileName(filePath: string): string {
  const segments = filePath.split(/[\\/]/)
  return segments[segments.length - 1] || filePath
}

function buildLocalPreview(filePath: string): VideoInfo {
  const fileName = getFileName(filePath)
  return {
    id: `local-${Date.now()}`,
    title: fileName,
    description: filePath,
    duration: 0,
    thumbnail: '',
    author: 'Local file',
  }
}

function parseSpeakerCount(value: string): number | undefined {
  if (!value.trim()) {
    return undefined
  }

  const parsed = Number(value)
  if (!Number.isInteger(parsed) || parsed < 1) {
    return undefined
  }

  return parsed
}

export function VideoInputStep({ initialInput = '', initialSpeakerCount, onSubmit }: VideoInputStepProps) {
  const initialSourceType = guessInitialSourceType(initialInput)
  const [sourceType, setSourceType] = useState<MediaSourceType>(initialSourceType)

  const [url, setUrl] = useState(initialSourceType === 'youtube' ? initialInput : '')
  const [speakerCount, setSpeakerCount] = useState(initialSpeakerCount?.toString() ?? '')
  const [isLoading, setIsLoading] = useState(false)
  const [videoInfo, setVideoInfo] = useState<VideoInfo | null>(null)

  const [localPath, setLocalPath] = useState(initialSourceType === 'local' ? initialInput : '')
  const localPreview = useMemo(() => (localPath ? buildLocalPreview(localPath) : null), [localPath])

  const [error, setError] = useState('')

  const handleFetchInfo = async () => {
    if (!url.trim()) return

    setIsLoading(true)
    setError('')

    try {
      const info = await window.electronAPI.getVideoInfo(url.trim())
      setVideoInfo(info)
    } catch (err) {
      setError(err instanceof Error ? err.message : '获取 YouTube 信息失败')
    } finally {
      setIsLoading(false)
    }
  }

  const handleSelectLocalFile = async () => {
    setError('')
    try {
      const selected = await window.electronAPI.selectFile(mediaFileFilters)
      if (selected) {
        setLocalPath(selected)
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : '选择本地文件失败')
    }
  }

  const handleSubmit = () => {
    if (sourceType === 'youtube') {
      if (!videoInfo || !url.trim()) return
      onSubmit({
        sourceType: 'youtube',
        source: url.trim(),
        speakerCount: parseSpeakerCount(speakerCount),
        previewInfo: videoInfo,
      })
      return
    }

    if (!localPath || !localPreview) return
    onSubmit({
      sourceType: 'local',
      source: localPath,
      speakerCount: parseSpeakerCount(speakerCount),
      previewInfo: localPreview,
    })
  }

  return (
    <div className="max-w-2xl mx-auto space-y-6">
      <div className="text-center">
        <h2 className="text-2xl font-bold">选择媒体来源</h2>
        <p className="text-muted-foreground mt-2">
          支持 YouTube 链接，或本地视频/音频文件。
        </p>
      </div>

      <Tabs
        value={sourceType}
        onValueChange={(value) => {
          setSourceType(value as MediaSourceType)
          setError('')
        }}
      >
        <TabsList className="grid w-full grid-cols-2">
          <TabsTrigger value="youtube">YouTube</TabsTrigger>
          <TabsTrigger value="local">本地文件</TabsTrigger>
        </TabsList>

        <TabsContent value="youtube" className="space-y-4">
          <div className="flex gap-2">
            <Input
              placeholder="https://www.youtube.com/watch?v=..."
              value={url}
              onChange={(event) => {
                setUrl(event.target.value)
                setVideoInfo(null)
                setError('')
              }}
              className="flex-1"
            />
            <Button onClick={handleFetchInfo} disabled={isLoading || !url.trim()}>
              {isLoading ? <Loader2 className="h-4 w-4 animate-spin" /> : '获取信息'}
            </Button>
          </div>
        </TabsContent>

        <TabsContent value="local" className="space-y-4">
          <Button variant="outline" className="w-full" onClick={handleSelectLocalFile}>
            <Upload className="h-4 w-4 mr-2" />
            选择本地视频或音频
          </Button>
          <Input value={localPath} readOnly placeholder="尚未选择文件" />
        </TabsContent>
      </Tabs>

      <div className="space-y-2">
        <label className="text-sm font-medium" htmlFor="speaker-count">
          说话人数量（可选）
        </label>
        <Input
          id="speaker-count"
          type="number"
          min={1}
          step={1}
          inputMode="numeric"
          placeholder="自动识别"
          value={speakerCount}
          onChange={(event) => {
            setSpeakerCount(event.target.value)
          }}
        />
      </div>

      {error && (
        <div className="text-destructive text-sm">{error}</div>
      )}

      {sourceType === 'youtube' && videoInfo && (
        <div className="border rounded-lg p-4 space-y-4">
          <div className="flex gap-4">
            {videoInfo.thumbnail && (
              <img
                src={videoInfo.thumbnail}
                alt={videoInfo.title}
                className="w-48 h-27 object-cover rounded"
              />
            )}
            <div className="flex-1">
              <h3 className="font-semibold text-lg">{videoInfo.title}</h3>
              <p className="text-muted-foreground text-sm mt-1">{videoInfo.author}</p>
              <p className="text-muted-foreground text-sm">时长: {formatDuration(videoInfo.duration)}</p>
            </div>
          </div>

          {videoInfo.description && (
            <div className="text-sm text-muted-foreground line-clamp-3">{videoInfo.description}</div>
          )}
        </div>
      )}

      {sourceType === 'local' && localPreview && (
        <div className="border rounded-lg p-4 space-y-2">
          <h3 className="font-semibold text-lg">{localPreview.title}</h3>
          <p className="text-muted-foreground text-sm break-all">{localPreview.description}</p>
          <p className="text-muted-foreground text-sm">来源: 本地文件</p>
        </div>
      )}

      <Button
        className="w-full"
        onClick={handleSubmit}
        disabled={sourceType === 'youtube' ? !videoInfo : !localPath}
      >
        开始处理
      </Button>
    </div>
  )
}

function formatDuration(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds <= 0) {
    return '未知'
  }

  const hours = Math.floor(seconds / 3600)
  const minutes = Math.floor((seconds % 3600) / 60)
  const secs = Math.floor(seconds % 60)

  if (hours > 0) {
    return `${hours}:${minutes.toString().padStart(2, '0')}:${secs.toString().padStart(2, '0')}`
  }
  return `${minutes}:${secs.toString().padStart(2, '0')}`
}
