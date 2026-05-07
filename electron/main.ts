import { app, BrowserWindow, ipcMain, dialog } from 'electron'
import * as path from 'path'
import { spawn, ChildProcess } from 'child_process'
import * as http from 'http'
import Store from 'electron-store'
import { downloadAudio, getVideoInfo } from './services/youtube'
import { PythonBridge } from './services/python-bridge'

const store = new Store()
let pythonProcess: ChildProcess | null = null
let pythonBridge: PythonBridge | null = null

interface StoredSettings {
  whisper?: {
    modelPath?: string
    device?: string
  }
  diarization?: {
    modelPath?: string
    hfToken?: string
  }
  output?: {
    defaultDir?: string
  }
}

function resolveAudioOutputDir(outputDir?: string): string {
  const trimmedOutputDir = outputDir?.trim()
  if (trimmedOutputDir) {
    return trimmedOutputDir
  }

  const settings = store.get('settings') as StoredSettings | undefined
  const defaultDir = settings?.output?.defaultDir?.trim()
  if (defaultDir) {
    return defaultDir
  }

  const fallbackDir = path.join(app.getPath('userData'), 'downloads')
  const fs = require('fs')
  if (!fs.existsSync(fallbackDir)) {
    fs.mkdirSync(fallbackDir, { recursive: true })
  }
  return fallbackDir
}

function createWindow() {
  const mainWindow = new BrowserWindow({
    width: 1400,
    height: 900,
    minWidth: 1000,
    minHeight: 700,
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
    },
    titleBarStyle: 'hiddenInset',
    trafficLightPosition: { x: 15, y: 15 },
  })

  if (!app.isPackaged) {
    mainWindow.loadURL('http://localhost:5173')
    mainWindow.webContents.openDevTools()
  } else {
    mainWindow.loadFile(path.join(__dirname, '../dist/index.html'))
  }
}

function healthCheck(port: number, timeoutMs = 5000): Promise<boolean> {
  return new Promise((resolve) => {
    const req = http.get(`http://127.0.0.1:${port}/health`, (res) => {
      resolve(res.statusCode === 200)
    })
    req.on('error', () => resolve(false))
    req.setTimeout(timeoutMs, () => {
      req.destroy()
      resolve(false)
    })
  })
}

async function waitForBackend(port: number, maxWaitMs = 30000): Promise<boolean> {
  const start = Date.now()
  while (Date.now() - start < maxWaitMs) {
    const ok = await healthCheck(port, 2000)
    if (ok) return true
    await new Promise((r) => setTimeout(r, 500))
  }
  return false
}

// 启动Python服务
async function startPythonService(): Promise<number> {
  const isDev = !app.isPackaged

  if (isDev) {
    const pythonPath = path.join(__dirname, '../python/server.py')
    return new Promise((resolve, reject) => {
      pythonProcess = spawn('python', ['-X', 'utf8', pythonPath], {
        cwd: path.dirname(pythonPath),
        windowsHide: true,
        shell: false,
        env: {
          ...process.env,
          PYTHONIOENCODING: 'utf-8',
        },
      })

      pythonProcess.stdout?.on('data', (data) => {
        const output = data.toString()
        console.log(`Python: ${output}`)
        const portMatch = output.match(/Running on.*:(\d+)/)
        if (portMatch) {
          resolve(parseInt(portMatch[1]))
        }
      })

      pythonProcess.stderr?.on('data', (data) => {
        console.error(`Python Error: ${data.toString()}`)
      })

      pythonProcess.on('error', (err) => {
        console.error('Failed to start Python service:', err)
        reject(err)
      })

      pythonProcess.on('close', (code) => {
        if (code !== 0 && code !== null) {
          reject(new Error(`Python process exited with code ${code}`))
        }
      })

      setTimeout(() => resolve(8000), 2000)
    })
  }

  // 生产环境：启动打包好的 backend.exe
  const backendExe = path.join(process.resourcesPath, 'backend', 'backend.exe')
  const settings = (store.get('settings') as StoredSettings) || {}
  const modelsDir = path.join(path.dirname(app.getPath('exe')), 'models')

  const whisperModelPath = settings.whisper?.modelPath?.trim() || path.join(modelsDir, 'whisper')
  const diarizationModelPath = settings.diarization?.modelPath?.trim() || path.join(modelsDir, 'diarization')
  const hfToken = settings.diarization?.hfToken || process.env.HF_TOKEN || ''

  let stderrLog = ''

  return new Promise((resolve, reject) => {
    pythonProcess = spawn(backendExe, [], {
      windowsHide: false,
      shell: false,
      env: {
        ...process.env,
        PYTHONIOENCODING: 'utf-8',
        WHISPER_MODEL_PATH: whisperModelPath,
        DIARIZATION_MODEL_PATH: diarizationModelPath,
        HF_TOKEN: hfToken,
        PYTHON_SERVER_PORT: '8000',
      },
    })

    pythonProcess.stdout?.on('data', (data) => {
      const output = data.toString()
      console.log(`Backend: ${output}`)
      const portMatch = output.match(/Running on.*:(\d+)/)
      if (portMatch) {
        resolve(parseInt(portMatch[1]))
      }
    })

    pythonProcess.stderr?.on('data', (data) => {
      const msg = data.toString()
      stderrLog += msg
      console.error(`Backend Error: ${msg}`)
    })

    pythonProcess.on('error', (err) => {
      reject(new Error(`无法启动后端进程: ${err.message}`))
    })

    pythonProcess.on('close', (code) => {
      if (code !== 0 && code !== null) {
        const summary = stderrLog.slice(-500) || '(无错误输出)'
        reject(new Error(`后端进程异常退出 (code ${code}):\n${summary}`))
      }
    })

    // 用健康检查轮询确认后端真正就绪（最多等 30 秒）
    waitForBackend(8000, 30000).then((ok) => {
      if (ok) {
        resolve(8000)
      }
      // 如果 30 秒后仍未就绪，让 close 事件的 reject 来处理
    })
  })
}

// 应用启动
app.whenReady().then(async () => {
  try {
    const port = await startPythonService()
    pythonBridge = new PythonBridge(port)
    createWindow()
  } catch (error) {
    console.error('Failed to start app:', error)
    dialog.showErrorBox('启动失败', `无法启动后端服务: ${error}`)
  }

  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) {
      createWindow()
    }
  })
})

app.on('window-all-closed', () => {
  if (pythonProcess) {
    pythonProcess.kill()
  }
  if (process.platform !== 'darwin') {
    app.quit()
  }
})

// IPC处理程序
ipcMain.handle('get-video-info', async (_, url: string) => {
  return await getVideoInfo(url)
})

ipcMain.handle('download-audio', async (_, url: string, outputDir: string) => {
  return await downloadAudio(url, resolveAudioOutputDir(outputDir))
})

ipcMain.handle('transcribe', async (_, audioPath: string) => {
  return await pythonBridge?.transcribe(audioPath)
})

ipcMain.handle('get-transcription-progress', async (_, audioPath?: string) => {
  return await pythonBridge?.getTranscriptionProgress(audioPath)
})

ipcMain.handle('diarize', async (_, audioPath: string, numSpeakers?: number) => {
  return await pythonBridge?.diarize(audioPath, numSpeakers)
})

ipcMain.handle('get-diarization-progress', async (_, audioPath?: string) => {
  return await pythonBridge?.getDiarizationProgress(audioPath)
})

ipcMain.handle('translate', async (_, transcript: any, speakers: any, context: any, config: any) => {
  return await pythonBridge?.translate(transcript, speakers, context, config)
})

ipcMain.handle('preview-prompt', async (_, transcript: any, speakers: any, context: any) => {
  return await pythonBridge?.previewPrompt(transcript, speakers, context)
})

ipcMain.handle('get-hltv-info', async (_, query: string, type: 'team' | 'player') => {
  return await pythonBridge?.getHLTVInfo(query, type)
})

ipcMain.handle('get-bailian-models', async (_, apiKey: string) => {
  return await pythonBridge?.getBailianModels(apiKey)
})

ipcMain.handle('get-store', (_, key: string) => {
  return store.get(key)
})

ipcMain.handle('set-store', (_, key: string, value: any) => {
  store.set(key, value)
  return true
})

ipcMain.handle('select-directory', async () => {
  const result = await dialog.showOpenDialog({
    properties: ['openDirectory']
  })
  return result.filePaths[0] || null
})

ipcMain.handle('select-file', async (_, filters: any[]) => {
  const result = await dialog.showOpenDialog({
    properties: ['openFile'],
    filters
  })
  return result.filePaths[0] || null
})
