export type ProjectStep = 'video' | 'processing' | 'speakers' | 'context' | 'prompt' | 'article'

export type ProcessingPhase =
  | 'idle'
  | 'downloading'
  | 'transcribing'
  | 'diarizing'
  | 'completed'
  | 'error'

export interface ProjectProcessingState {
  phase: ProcessingPhase
  message: string
  progress: number
  audioPath?: string
  error?: string
  updatedAt: number
}

interface ProjectStepState {
  processingState?: ProjectProcessingState
  transcript?: unknown
  article?: string
}

interface ProjectUpdateTarget {
  id: string
  updatedAt: Date
}

export function isProcessingPhaseActive(phase: ProcessingPhase): boolean {
  return phase === 'downloading' || phase === 'transcribing' || phase === 'diarizing'
}

export function getProjectStep(project: ProjectStepState): ProjectStep {
  if (project.processingState?.phase && isProcessingPhaseActive(project.processingState.phase)) {
    return 'processing'
  }

  if (project.processingState?.phase === 'error') {
    return 'video'
  }

  if (project.article) {
    return 'article'
  }

  if (project.transcript) {
    return 'speakers'
  }

  return 'video'
}

export function applyProjectUpdate<TProject extends ProjectUpdateTarget>(
  projects: TProject[],
  currentProject: TProject | null,
  updates: Partial<TProject>
): { projects: TProject[]; currentProject: TProject | null } {
  if (!currentProject) {
    return { projects, currentProject }
  }

  const updatedProject = {
    ...currentProject,
    ...updates,
    updatedAt: new Date(),
  }

  return {
    currentProject: updatedProject,
    projects: projects.map((project) => (
      project.id === updatedProject.id ? updatedProject : project
    )),
  }
}

export function applyProjectUpdateById<TProject extends ProjectUpdateTarget>(
  projects: TProject[],
  currentProject: TProject | null,
  projectId: string,
  updates: Partial<TProject>
): { projects: TProject[]; currentProject: TProject | null } {
  const targetProject = projects.find((project) => project.id === projectId)
  if (!targetProject) {
    return { projects, currentProject }
  }

  const updatedProject = {
    ...targetProject,
    ...updates,
    updatedAt: new Date(),
  }

  return {
    currentProject: currentProject?.id === projectId ? updatedProject : currentProject,
    projects: projects.map((project) => (
      project.id === projectId ? updatedProject : project
    )),
  }
}
