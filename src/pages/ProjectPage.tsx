import { useEffect, useRef, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { ArrowLeft, Download, Mic, FileText, Sparkles, Save } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Progress } from '@/components/ui/progress'
import { useAppStore, type Project, type TranscriptSegment } from '@/stores/appStore'
import {
  getProjectStep,
  isProcessingPhaseActive,
  type ProjectProcessingState,
  type ProjectStep,
} from '@/stores/projectProcessing'
import { VideoInputStep, type MediaSubmitPayload } from '@/components/project/VideoInputStep'
import { SpeakerLabelingStep } from '@/components/project/SpeakerLabelingStep'
import { ContextStep } from '@/components/project/ContextStep'
import { PromptStep } from '@/components/project/PromptStep'
import { ArticleEditor } from '@/components/project/ArticleEditor'

interface DiarizationSegment {
  speaker: string
  start: number
  end: number
}

interface TranscriptionProgressState {
  status: 'idle' | 'running' | 'completed' | 'error'
  progress: number
  message: string
}

interface DiarizationProgressState {
  status: 'idle' | 'running' | 'completed' | 'error'
  progress: number
  message: string
}

function createNewProject(): Project {
  return {
    id: Date.now().toString(),
    name: '新项目',
    videoUrl: '',
    speakers: {},
    createdAt: new Date(),
    updatedAt: new Date(),
  }
}

export function ProjectPage() {
  const { id } = useParams()
  const navigate = useNavigate()
  const {
    projects,
    currentProject,
    setCurrentProject,
    updateProject,
    updateProjectById,
    settings,
    processingStep,
    processingProgress,
    setProcessing,
  } = useAppStore()
  const [step, setStep] = useState<ProjectStep>('video')
  const [promptDraft, setPromptDraft] = useState('')
  const [isPromptLoading, setIsPromptLoading] = useState(false)
  const [promptError, setPromptError] = useState<string | null>(null)
  const [isGeneratingArticle, setIsGeneratingArticle] = useState(false)
  const isMountedRef = useRef(true)
  const transcriptionProgressTimerRef = useRef<{ projectId: string; timerId: number } | null>(null)
  const diarizationProgressTimerRef = useRef<{ projectId: string; timerId: number } | null>(null)

  const stopTranscriptionProgressPolling = (projectId?: string) => {
    const timer = transcriptionProgressTimerRef.current
    if (timer && (!projectId || timer.projectId === projectId)) {
      window.clearInterval(timer.timerId)
      transcriptionProgressTimerRef.current = null
    }
  }

  const stopDiarizationProgressPolling = (projectId?: string) => {
    const timer = diarizationProgressTimerRef.current
    if (timer && (!projectId || timer.projectId === projectId)) {
      window.clearInterval(timer.timerId)
      diarizationProgressTimerRef.current = null
    }
  }

  const stopProgressPolling = (projectId?: string) => {
    stopTranscriptionProgressPolling(projectId)
    stopDiarizationProgressPolling(projectId)
  }

  const isProjectVisible = (projectId?: string) => {
    if (!isMountedRef.current) {
      return false
    }
    if (!projectId) {
      return true
    }
    return useAppStore.getState().currentProject?.id === projectId
  }

  const setMountedProcessing = (message: string, progress: number, projectId?: string) => {
    if (isProjectVisible(projectId)) {
      setProcessing(true, message, progress)
    }
  }

  const setMountedStep = (nextStep: ProjectStep, projectId?: string) => {
    if (isProjectVisible(projectId)) {
      setStep(nextStep)
    }
  }

  const saveProcessingState = (
    projectId: string,
    state: Omit<ProjectProcessingState, 'updatedAt'>,
  ) => {
    const processingState = {
      ...state,
      updatedAt: Date.now(),
    }
    const updates: Partial<Project> = { processingState }
    if (state.audioPath) {
      updates.audioPath = state.audioPath
    }
    updateProjectById(projectId, updates)
  }

  const syncTranscriptionProgress = (
    projectId: string,
    audioPath: string,
    snapshot: TranscriptionProgressState
  ) => {
    const scaledProgress = Math.max(5, Math.min(88, Math.round(snapshot.progress * 0.88)))
    const nextStep = snapshot.message || '正在转写音频...'

    if (snapshot.status === 'completed') {
      saveProcessingState(projectId, {
        phase: 'transcribing',
        message: '转写完成，准备说话人分离...',
        progress: 90,
        audioPath,
      })
      setMountedProcessing('转写完成，准备说话人分离...', 90, projectId)
      stopTranscriptionProgressPolling(projectId)
      return
    }

    if (snapshot.status === 'error') {
      const message = snapshot.message || '转写失败'
      saveProcessingState(projectId, {
        phase: 'error',
        message,
        progress: scaledProgress,
        audioPath,
        error: message,
      })
      stopTranscriptionProgressPolling(projectId)
      setMountedStep('video', projectId)
      if (isProjectVisible(projectId)) {
        setProcessing(false, '', 0)
      }
      return
    }

    saveProcessingState(projectId, {
      phase: 'transcribing',
      message: nextStep,
      progress: scaledProgress,
      audioPath,
    })
    setMountedProcessing(nextStep, scaledProgress, projectId)
  }

  const startTranscriptionProgressPolling = (audioPath: string, projectId: string) => {
    stopTranscriptionProgressPolling()

    const poll = async () => {
      try {
        const snapshot = await window.electronAPI.getTranscriptionProgress(audioPath)
        syncTranscriptionProgress(projectId, audioPath, snapshot)
      } catch (error) {
        console.warn('获取转写进度失败:', error)
      }
    }

    void poll()
    transcriptionProgressTimerRef.current = {
      projectId,
      timerId: window.setInterval(() => {
        void poll()
      }, 1000),
    }
  }

  const syncDiarizationProgress = (
    projectId: string,
    audioPath: string,
    snapshot: DiarizationProgressState
  ) => {
    const scaledProgress = Math.max(90, Math.min(98, Math.round(90 + snapshot.progress * 0.08)))
    const nextStep = snapshot.message || '正在进行说话人分离...'

    if (snapshot.status === 'completed') {
      saveProcessingState(projectId, {
        phase: 'diarizing',
        message: '说话人分离完成，正在整理结果...',
        progress: 98,
        audioPath,
      })
      setMountedProcessing('说话人分离完成，正在整理结果...', 98, projectId)
      stopDiarizationProgressPolling(projectId)
      return
    }

    if (snapshot.status === 'error') {
      const message = snapshot.message || '说话人分离失败'
      saveProcessingState(projectId, {
        phase: 'error',
        message,
        progress: scaledProgress,
        audioPath,
        error: message,
      })
      stopDiarizationProgressPolling(projectId)
      setMountedStep('video', projectId)
      if (isProjectVisible(projectId)) {
        setProcessing(false, '', 0)
      }
      return
    }

    saveProcessingState(projectId, {
      phase: 'diarizing',
      message: nextStep,
      progress: scaledProgress,
      audioPath,
    })
    setMountedProcessing(nextStep, scaledProgress, projectId)
  }

  const startDiarizationProgressPolling = (audioPath: string, projectId: string) => {
    stopDiarizationProgressPolling()

    const poll = async () => {
      try {
        const snapshot = await window.electronAPI.getDiarizationProgress(audioPath)
        syncDiarizationProgress(projectId, audioPath, snapshot)
      } catch (error) {
        console.warn('获取说话人分离进度失败:', error)
      }
    }

    void poll()
    diarizationProgressTimerRef.current = {
      projectId,
      timerId: window.setInterval(() => {
        void poll()
      }, 1000),
    }
  }

  useEffect(() => {
    if (id === 'new') {
      const project = createNewProject()
      setCurrentProject(project)
      setStep('video')
      return () => setCurrentProject(null)
    }

    const project = projects.find((item) => item.id === id)
    if (!project) {
      navigate('/')
      return () => setCurrentProject(null)
    }

    setCurrentProject(project)
    const restoredStep = getProjectStep(project)
    setStep(restoredStep)

    if (restoredStep === 'processing' && project.processingState) {
      setProcessing(true, project.processingState.message, project.processingState.progress)
    } else {
      setProcessing(false, '', 0)
    }

    return () => setCurrentProject(null)
  }, [id, navigate, setCurrentProject, setProcessing])

  useEffect(() => () => {
    isMountedRef.current = false
    stopProgressPolling()
  }, [])

  useEffect(() => {
    const processingState = currentProject?.processingState
    const audioPath = processingState?.audioPath || currentProject?.audioPath

    if (!currentProject || !processingState || !audioPath || !isProcessingPhaseActive(processingState.phase)) {
      return
    }

    setStep('processing')
    setProcessing(true, processingState.message, processingState.progress)

    if (processingState.phase === 'transcribing') {
      startTranscriptionProgressPolling(audioPath, currentProject.id)
      return () => stopTranscriptionProgressPolling(currentProject.id)
    }

    if (processingState.phase === 'diarizing') {
      startDiarizationProgressPolling(audioPath, currentProject.id)
      return () => stopDiarizationProgressPolling(currentProject.id)
    }
  }, [
    currentProject?.id,
    currentProject?.audioPath,
    currentProject?.processingState?.phase,
    currentProject?.processingState?.audioPath,
    setProcessing,
  ])

  const handleMediaSubmit = async (payload: MediaSubmitPayload) => {
    const activeProject = useAppStore.getState().currentProject ?? currentProject
    if (!activeProject) {
      return
    }

    const projectId = activeProject.id
    let audioPath = payload.source

    try {
      setMountedStep('processing', projectId)
      const initialMessage = payload.sourceType === 'youtube' ? '正在获取视频信息...' : '准备处理音频...'
      setMountedProcessing(initialMessage, 5, projectId)
      saveProcessingState(projectId, {
        phase: payload.sourceType === 'youtube' ? 'downloading' : 'transcribing',
        message: initialMessage,
        progress: 5,
        audioPath: payload.sourceType === 'local' ? audioPath : undefined,
      })

      let videoInfo = payload.previewInfo

      if (payload.sourceType === 'youtube') {
        setMountedProcessing('正在获取视频信息...', 8, projectId)
        saveProcessingState(projectId, {
          phase: 'downloading',
          message: '正在获取视频信息...',
          progress: 8,
        })
        videoInfo = await window.electronAPI.getVideoInfo(payload.source)
        updateProjectById(projectId, {
          videoUrl: payload.source,
          videoInfo,
        })
        setMountedProcessing('正在下载音频...', 15, projectId)
        saveProcessingState(projectId, {
          phase: 'downloading',
          message: '正在下载音频...',
          progress: 15,
        })
        audioPath = await window.electronAPI.downloadAudio(payload.source, settings.output.defaultDir)
        setMountedProcessing('音频下载完成，准备转写...', 20, projectId)
        saveProcessingState(projectId, {
          phase: 'transcribing',
          message: '音频下载完成，准备转写...',
          progress: 20,
          audioPath,
        })
      }

      updateProjectById(projectId, {
        videoUrl: payload.source,
        videoInfo,
        audioPath,
        speakerCount: payload.speakerCount,
        processingState: {
          phase: 'transcribing',
          message: '音频准备完成，正在等待转写开始...',
          progress: 20,
          audioPath,
          updatedAt: Date.now(),
        },
      })

      if (isProjectVisible(projectId)) {
        startTranscriptionProgressPolling(audioPath, projectId)
      }
      const transcript = await window.electronAPI.transcribe(audioPath)
      stopTranscriptionProgressPolling(projectId)
      setMountedProcessing('转写完成，正在进行说话人分离...', 92, projectId)
      saveProcessingState(projectId, {
        phase: 'diarizing',
        message: '转写完成，正在进行说话人分离...',
        progress: 92,
        audioPath,
      })
      if (isProjectVisible(projectId)) {
        startDiarizationProgressPolling(audioPath, projectId)
      }
      const diarization = await window.electronAPI.diarize(audioPath, payload.speakerCount)
      stopDiarizationProgressPolling(projectId)
      setMountedProcessing('说话人分离完成，正在整理结果...', 98, projectId)
      saveProcessingState(projectId, {
        phase: 'diarizing',
        message: '说话人分离完成，正在整理结果...',
        progress: 98,
        audioPath,
      })

      const transcriptSegments = Array.isArray(transcript?.segments) ? transcript.segments : []
      const diarizationSegments = Array.isArray(diarization?.segments) ? diarization.segments : []
      const mergedTranscript = mergeTranscriptWithDiarization(transcriptSegments, diarizationSegments)

      updateProjectById(projectId, {
        transcript: mergedTranscript,
        processingState: {
          phase: 'completed',
          message: '处理完成',
          progress: 100,
          audioPath,
          updatedAt: Date.now(),
        },
      })

      if (isProjectVisible(projectId)) {
        setProcessing(false, '', 0)
        setStep('speakers')
      }
    } catch (error) {
      stopProgressPolling(projectId)
      const message = error instanceof Error ? error.message : String(error)
      const existingProject = useAppStore.getState().projects.find((project) => project.id === projectId)
      saveProcessingState(projectId, {
        phase: 'error',
        message: `处理失败: ${message}`,
        progress: existingProject?.processingState?.progress ?? 0,
        audioPath: existingProject?.audioPath || audioPath,
        error: message,
      })
      console.error('处理失败:', error)
      if (isProjectVisible(projectId)) {
        setProcessing(false, '', 0)
        alert(`处理失败: ${message}`)
        setStep('video')
      }
    }
  }

  const handleSpeakersSubmit = (speakers: Record<string, string>) => {
    updateProject({ speakers })
    setStep('context')
  }

  const handleContextSubmit = (context: Project['context']) => {
    updateProject({ context })
    void openPromptStep(context)
  }

  const loadPromptPreview = async (contextOverride?: Project['context']) => {
    setIsPromptLoading(true)
    setPromptError(null)
    try {
      const { currentProject: activeProject } = useAppStore.getState()
      if (!activeProject?.transcript) {
        return
      }
      const prompt = await window.electronAPI.previewPrompt(
        { segments: activeProject.transcript },
        activeProject.speakers,
        contextOverride ?? activeProject.context ?? {}
      )
      setPromptDraft(prompt)
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error)
      setPromptError(message)
      setPromptDraft('')
    } finally {
      setIsPromptLoading(false)
    }
  }

  const openPromptStep = async (contextOverride?: Project['context']) => {
    setStep('prompt')
    await loadPromptPreview(contextOverride)
  }

  const generateArticle = async (promptOverride?: string) => {
    setStep('article')
    setIsGeneratingArticle(true)

    try {
      const { currentProject: activeProject, settings: activeSettings } = useAppStore.getState()
      if (!activeProject?.transcript) {
        return
      }

      const llmConfig = promptOverride?.trim()
        ? { ...activeSettings.llm, promptOverride }
        : activeSettings.llm

      const article = await window.electronAPI.translate(
        { segments: activeProject.transcript },
        activeProject.speakers,
        activeProject.context || {},
        llmConfig
      )

      updateProject({ article })
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error)
      console.error('生成失败:', error)
      alert(`生成失败: ${message}`)
    } finally {
      setIsGeneratingArticle(false)
    }
  }

  const handleSave = () => {
    const { projects: allProjects, currentProject: activeProject } = useAppStore.getState()
    if (!activeProject) {
      return
    }

    const existingIndex = allProjects.findIndex((project) => project.id === activeProject.id)
    if (existingIndex >= 0) {
      const updatedProjects = [...allProjects]
      updatedProjects[existingIndex] = activeProject
      useAppStore.setState({ projects: updatedProjects })
    } else {
      useAppStore.setState({ projects: [...allProjects, activeProject] })
    }

    navigate('/')
  }

  const steps = [
    { id: 'video', label: '媒体输入', icon: Download },
    { id: 'processing', label: '处理中', icon: Mic },
    { id: 'speakers', label: '标注说话人', icon: Mic },
    { id: 'context', label: '补充信息', icon: FileText },
    { id: 'prompt', label: '确认提示词', icon: FileText },
    { id: 'article', label: '新闻稿', icon: Sparkles },
  ] satisfies Array<{ id: ProjectStep; label: string; icon: typeof Download }>

  const currentStepIndex = steps.findIndex((item) => item.id === step)

  if (!currentProject) {
    return <div>加载中...</div>
  }

  return (
    <div className="space-y-6">
      <div className="flex items-center gap-4">
        <Button variant="ghost" size="icon" onClick={() => navigate('/')}>
          <ArrowLeft className="h-5 w-5" />
        </Button>
        <div className="flex-1">
          <Input
            value={currentProject.name}
            onChange={(event) => updateProject({ name: event.target.value })}
            className="text-xl font-bold border-none p-0 h-auto focus-visible:ring-0"
          />
        </div>
        <Button onClick={handleSave}>
          <Save className="h-4 w-4 mr-2" />
          保存
        </Button>
      </div>

      <div className="flex items-center justify-between">
        {steps.map((item, index) => (
          <div
            key={item.id}
            className={`flex items-center ${index <= currentStepIndex ? 'text-primary' : 'text-muted-foreground'}`}
          >
            <div className={`w-8 h-8 rounded-full flex items-center justify-center border ${
              index <= currentStepIndex ? 'border-primary bg-primary/10' : 'border-muted'
            }`}>
              <item.icon className="h-4 w-4" />
            </div>
            <span className="ml-2 text-sm hidden md:inline">{item.label}</span>
            {index < steps.length - 1 && (
              <div className={`w-12 md:w-24 h-0.5 mx-2 ${
                index < currentStepIndex ? 'bg-primary' : 'bg-muted'
              }`} />
            )}
          </div>
        ))}
      </div>

      <div className="mt-8">
        {step === 'video' && (
          <VideoInputStep
            initialInput={currentProject.videoUrl}
            initialSpeakerCount={currentProject.speakerCount}
            onSubmit={handleMediaSubmit}
          />
        )}

        {step === 'processing' && (
          <div className="max-w-md mx-auto space-y-4 text-center">
            <h2 className="text-xl font-semibold">正在处理...</h2>
            <p className="text-muted-foreground">{processingStep || '转写与说话人分离中'}</p>
            <Progress value={processingProgress} />
          </div>
        )}

        {step === 'speakers' && currentProject.transcript && (
          <SpeakerLabelingStep
            transcript={currentProject.transcript}
            initialSpeakers={currentProject.speakers}
            onSubmit={handleSpeakersSubmit}
          />
        )}

        {step === 'context' && (
          <ContextStep
            initialContext={currentProject.context}
            onSubmit={handleContextSubmit}
            onSkip={() => {
              void openPromptStep()
            }}
          />
        )}

        {step === 'prompt' && currentProject.transcript && (
          <PromptStep
            prompt={promptDraft}
            isLoading={isPromptLoading}
            isGenerating={isGeneratingArticle}
            error={promptError}
            onPromptChange={setPromptDraft}
            onBack={() => setStep('context')}
            onRetry={() => {
              void loadPromptPreview()
            }}
            onGenerate={() => {
              void generateArticle(promptDraft)
            }}
          />
        )}

        {step === 'article' && (
          <ArticleEditor
            article={currentProject.article || ''}
            onChange={(article) => updateProject({ article })}
            onRegenerate={() => {
              void openPromptStep()
            }}
          />
        )}
      </div>
    </div>
  )
}

function mergeTranscriptWithDiarization(
  transcript: TranscriptSegment[],
  diarization: DiarizationSegment[]
): TranscriptSegment[] {
  const labeledSegments = transcript.map((segment) => {
    let bestSpeaker = 'Unknown'
    let bestOverlap = 0

    for (const item of diarization) {
      const overlapStart = Math.max(segment.start, item.start)
      const overlapEnd = Math.min(segment.end, item.end)
      const overlap = Math.max(0, overlapEnd - overlapStart)

      if (overlap > bestOverlap) {
        bestOverlap = overlap
        bestSpeaker = item.speaker
      }
    }

    return {
      ...segment,
      speaker: bestSpeaker,
    }
  })

  if (labeledSegments.length === 0) {
    return []
  }

  const mergedSegments: TranscriptSegment[] = [{ ...labeledSegments[0] }]

  for (const segment of labeledSegments.slice(1)) {
    const lastSegment = mergedSegments[mergedSegments.length - 1]
    if (segment.speaker === lastSegment.speaker) {
      lastSegment.end = segment.end
      lastSegment.text = `${lastSegment.text} ${segment.text}`.replace(/\s+/g, ' ').trim()
    } else {
      mergedSegments.push({ ...segment })
    }
  }

  return mergedSegments
}
