import { useNavigate } from 'react-router-dom'
import { Plus, Trash2, Clock } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { useAppStore, Project } from '@/stores/appStore'
import { isProcessingPhaseActive } from '@/stores/projectProcessing'

export function HomePage() {
  const navigate = useNavigate()
  const { projects, addProject, removeProject } = useAppStore()

  const handleNewProject = () => {
    const newProject: Project = {
      id: Date.now().toString(),
      name: `新项目 ${projects.length + 1}`,
      videoUrl: '',
      speakers: {},
      createdAt: new Date(),
      updatedAt: new Date(),
    }
    addProject(newProject)
    navigate(`/project/${newProject.id}`)
  }

  const handleDeleteProject = (id: string, e: React.MouseEvent) => {
    e.stopPropagation()
    if (confirm('确定要删除这个项目吗？')) {
      removeProject(id)
    }
  }

  const formatDate = (date: Date) => {
    return new Date(date).toLocaleDateString('zh-CN', {
      year: 'numeric',
      month: 'short',
      day: 'numeric',
      hour: '2-digit',
      minute: '2-digit',
    })
  }

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold">项目列表</h1>
          <p className="text-muted-foreground">管理你的翻译项目</p>
        </div>
        <Button onClick={handleNewProject}>
          <Plus className="h-4 w-4 mr-2" />
          新建项目
        </Button>
      </div>

      {projects.length === 0 ? (
        <div className="text-center py-12 border rounded-lg border-dashed">
          <div className="text-muted-foreground mb-4">还没有项目</div>
          <Button onClick={handleNewProject}>
            <Plus className="h-4 w-4 mr-2" />
            创建第一个项目
          </Button>
        </div>
      ) : (
        <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-3">
          {projects.map((project) => (
            <div
              key={project.id}
              onClick={() => navigate(`/project/${project.id}`)}
              className="border rounded-lg p-4 cursor-pointer hover:border-primary transition-colors group"
            >
              <div className="flex items-start justify-between">
                <div className="flex-1 min-w-0">
                  <h3 className="font-medium truncate">{project.name}</h3>
                  {project.videoInfo && (
                    <p className="text-sm text-muted-foreground truncate mt-1">
                      {project.videoInfo.title}
                    </p>
                  )}
                </div>
                <Button
                  variant="ghost"
                  size="icon"
                  className="opacity-0 group-hover:opacity-100 transition-opacity"
                  onClick={(e) => handleDeleteProject(project.id, e)}
                >
                  <Trash2 className="h-4 w-4 text-destructive" />
                </Button>
              </div>

              <div className="flex items-center gap-4 mt-4 text-sm text-muted-foreground">
                <div className="flex items-center gap-1">
                  <Clock className="h-3 w-3" />
                  {formatDate(project.updatedAt)}
                </div>
                {project.transcript && (
                  <span className="text-green-600">已转录</span>
                )}
                {project.article && (
                  <span className="text-blue-600">已生成</span>
                )}
                {project.processingState && isProcessingPhaseActive(project.processingState.phase) && (
                  <span className="text-amber-600">处理中 {project.processingState.progress}%</span>
                )}
                {project.processingState?.phase === 'error' && (
                  <span className="text-destructive">处理失败</span>
                )}
              </div>

              {project.videoInfo?.thumbnail && (
                <img
                  src={project.videoInfo.thumbnail}
                  alt=""
                  className="mt-3 w-full h-24 object-cover rounded"
                />
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
