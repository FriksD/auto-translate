import { create } from 'zustand'
import {
  applyProjectUpdate,
  applyProjectUpdateById,
  type ProjectProcessingState,
} from './projectProcessing'

export interface TranscriptSegment {
  start: number
  end: number
  text: string
  speaker?: string
}

export interface VideoInfo {
  id: string
  title: string
  description: string
  duration: number
  thumbnail: string
  author: string
}

export interface Project {
  id: string
  name: string
  videoUrl: string
  videoInfo?: VideoInfo
  audioPath?: string
  speakerCount?: number
  transcript?: TranscriptSegment[]
  processingState?: ProjectProcessingState
  speakers: Record<string, string>
  context?: {
    teams?: any[]
    players?: any[]
    customContext?: string
  }
  article?: string
  createdAt: Date
  updatedAt: Date
}

export interface AppSettings {
  llm: {
    provider: 'openai' | 'anthropic' | 'openrouter' | 'local' | 'bailian' | 'gemini' | 'relay_openai' | 'relay_anthropic'
    apiKey: string
    model: string
    baseUrl?: string
  }
  whisper: {
    device: 'cuda' | 'cpu'
    modelPath: string
  }
  diarization: {
    modelPath: string
    hfToken: string
  }
  output: {
    defaultDir: string
    format: 'markdown' | 'html' | 'txt'
  }
}

interface AppState {
  // 当前项目
  currentProject: Project | null
  projects: Project[]

  // 处理状态
  isProcessing: boolean
  processingStep: string
  processingProgress: number

  // 设置
  settings: AppSettings

  // Actions
  setCurrentProject: (project: Project | null) => void
  updateProject: (updates: Partial<Project>) => void
  updateProjectById: (projectId: string, updates: Partial<Project>) => void
  addProject: (project: Project) => void
  removeProject: (id: string) => void

  setProcessing: (isProcessing: boolean, step?: string, progress?: number) => void
  setSettings: (settings: Partial<AppSettings>) => void
}

const defaultSettings: AppSettings = {
  llm: {
    provider: 'bailian',
    apiKey: '',
    model: 'qwen-turbo',
  },
  whisper: {
    device: 'cuda',
    modelPath: '',
  },
  diarization: {
    modelPath: '',
    hfToken: '',
  },
  output: {
    defaultDir: '',
    format: 'markdown',
  },
}

export const useAppStore = create<AppState>((set, get) => ({
  currentProject: null,
  projects: [],
  isProcessing: false,
  processingStep: '',
  processingProgress: 0,
  settings: defaultSettings,

  setCurrentProject: (project) => set({ currentProject: project }),

  updateProject: (updates) => {
    const { projects, currentProject } = get()
    if (!currentProject) {
      return
    }

    set(applyProjectUpdate(projects, currentProject, updates))
  },

  updateProjectById: (projectId, updates) => {
    const { projects, currentProject } = get()
    set(applyProjectUpdateById(projects, currentProject, projectId, updates))
  },

  addProject: (project) => {
    const { projects } = get()
    set({ projects: [...projects, project] })
  },

  removeProject: (id) => {
    const { projects, currentProject } = get()
    set({
      projects: projects.filter((p) => p.id !== id),
      currentProject: currentProject?.id === id ? null : currentProject,
    })
  },

  setProcessing: (isProcessing, step = '', progress = 0) => {
    set({ isProcessing, processingStep: step, processingProgress: progress })
  },

  setSettings: (newSettings) => {
    const { settings } = get()
    set({ settings: { ...settings, ...newSettings } })
  },
}))
