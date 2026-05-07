import { execFile, spawn } from 'child_process'
import { app } from 'electron'
import { promisify } from 'util'
import * as path from 'path'
import * as fs from 'fs'

const execFileAsync = promisify(execFile)

export interface VideoInfo {
  id: string
  title: string
  description: string
  duration: number
  thumbnail: string
  author: string
}

interface YtDlpRunner {
  command: string
  baseArgs: string[]
}

const AUDIO_EXTENSIONS = new Set(['.m4a', '.webm', '.mp3', '.aac', '.opus', '.wav', '.flac'])

function getBundledBinDir(): string {
  if (app.isPackaged) {
    return path.join(process.resourcesPath, 'bin')
  }
  return path.join(__dirname, '..', 'resources', 'bin')
}

function buildYtDlpCandidates(): YtDlpRunner[] {
  const candidates: YtDlpRunner[] = []

  // 优先使用环境变量指定的路径
  if (process.env.YT_DLP_PATH) {
    candidates.push({ command: process.env.YT_DLP_PATH, baseArgs: [] })
  }

  // 生产环境优先使用内置 yt-dlp.exe
  const bundledBin = getBundledBinDir()
  const bundledYtDlp = path.join(bundledBin, 'yt-dlp.exe')
  if (fs.existsSync(bundledYtDlp)) {
    candidates.push({ command: bundledYtDlp, baseArgs: [] })
  }

  // 系统 PATH 中的 yt-dlp
  candidates.push({ command: 'yt-dlp', baseArgs: [] })

  // Python 模块方式
  candidates.push({ command: 'python', baseArgs: ['-m', 'yt_dlp'] })
  candidates.push({ command: 'py', baseArgs: ['-m', 'yt_dlp'] })

  return candidates
}

let cachedRunner: YtDlpRunner | null = null

function isCommandNotFound(error: unknown): boolean {
  const execError = error as NodeJS.ErrnoException
  return execError?.code === 'ENOENT'
}

function isPythonModuleMissing(error: unknown): boolean {
  const execError = error as NodeJS.ErrnoException & { stderr?: string }
  const stderr = execError?.stderr || ''
  return /No module named yt_dlp/i.test(stderr)
}

function isFfmpegMissing(error: unknown): boolean {
  const execError = error as NodeJS.ErrnoException & { stderr?: string }
  const stderr = execError?.stderr || ''
  const message = execError?.message || ''
  return /ffprobe and ffmpeg not found|ffmpeg not found/i.test(`${stderr}\n${message}`)
}

function getErrorMessage(error: unknown): string {
  if (error instanceof Error) {
    return error.message
  }
  return String(error)
}

function buildExtractAudioArgs(url: string, outputPath: string, withProgress = false): string[] {
  return [
    '--no-playlist',
    '-x',
    '--audio-format',
    'm4a',
    '--audio-quality',
    '0',
    '-o',
    outputPath,
    ...(withProgress ? ['--progress'] : []),
    '--print',
    'after_move:filepath',
    url,
  ]
}

function buildDirectAudioArgs(url: string, outputPath: string, withProgress = false): string[] {
  return [
    '--no-playlist',
    '-f',
    'bestaudio[ext=m4a]/bestaudio/best',
    '-o',
    outputPath,
    ...(withProgress ? ['--progress'] : []),
    '--print',
    'after_move:filepath',
    url,
  ]
}

function parseDownloadedFilePath(stdout: string): string | null {
  const lines = stdout
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter(Boolean)

  for (let index = lines.length - 1; index >= 0; index -= 1) {
    const possiblePath = lines[index]
    if (fs.existsSync(possiblePath)) {
      return possiblePath
    }
  }

  return null
}

function findNewestAudioFile(outputDir: string, minMtimeMs = 0): string | null {
  const candidates = fs
    .readdirSync(outputDir)
    .map((fileName) => {
      const fullPath = path.join(outputDir, fileName)
      const ext = path.extname(fileName).toLowerCase()
      return { fullPath, ext }
    })
    .filter(({ fullPath, ext }) => AUDIO_EXTENSIONS.has(ext) && fs.existsSync(fullPath))
    .map(({ fullPath }) => ({
      fullPath,
      mtimeMs: fs.statSync(fullPath).mtimeMs,
    }))
    .filter(({ mtimeMs }) => mtimeMs >= minMtimeMs)
    .sort((a, b) => b.mtimeMs - a.mtimeMs)

  return candidates[0]?.fullPath || null
}

function resolveDownloadedFile(stdout: string, outputDir: string, minMtimeMs: number): string {
  const printedPath = parseDownloadedFilePath(stdout)
  if (printedPath) {
    return printedPath
  }

  const fallbackFile = findNewestAudioFile(outputDir, minMtimeMs)
  if (fallbackFile) {
    return fallbackFile
  }

  throw new Error('Unable to locate the downloaded audio file')
}

async function resolveYtDlpRunner(): Promise<YtDlpRunner> {
  if (cachedRunner) {
    return cachedRunner
  }

  const candidates = buildYtDlpCandidates()
  let lastError: unknown

  for (const candidate of candidates) {
    try {
      await execFileAsync(candidate.command, [...candidate.baseArgs, '--version'], {
        windowsHide: true,
      })
      cachedRunner = candidate
      return candidate
    } catch (error) {
      lastError = error
      if (isCommandNotFound(error) || isPythonModuleMissing(error)) {
        continue
      }
      throw error
    }
  }

  throw new Error(
    `未找到可用的 yt-dlp。请先安装 yt-dlp，或确保 Python 环境安装了 yt_dlp 模块。${
      lastError ? ` 详情: ${getErrorMessage(lastError)}` : ''
    }`
  )
}

async function runYtDlp(args: string[], maxBuffer = 1024 * 1024 * 10): Promise<string> {
  const runner = await resolveYtDlpRunner()
  const { stdout } = await execFileAsync(runner.command, [...runner.baseArgs, ...args], {
    maxBuffer,
    windowsHide: true,
  })
  return stdout
}

export async function getVideoInfo(url: string): Promise<VideoInfo> {
  try {
    const stdout = await runYtDlp(['--no-playlist', '--dump-json', '--no-download', url])
    const info = JSON.parse(stdout)

    return {
      id: info.id,
      title: info.title,
      description: info.description || '',
      duration: info.duration,
      thumbnail: info.thumbnail,
      author: info.uploader || info.channel,
    }
  } catch (error) {
    throw new Error(`获取视频信息失败: ${getErrorMessage(error)}`)
  }
}

export async function downloadAudio(url: string, outputDir: string): Promise<string> {
  if (!fs.existsSync(outputDir)) {
    fs.mkdirSync(outputDir, { recursive: true })
  }

  const outputPath = path.join(outputDir, '%(id)s.%(ext)s')
  const startTime = Date.now()

  try {
    const stdout = await runYtDlp(buildExtractAudioArgs(url, outputPath), 1024 * 1024 * 10)
    return resolveDownloadedFile(stdout, outputDir, startTime)
  } catch (extractError) {
    if (!isFfmpegMissing(extractError)) {
      throw new Error(`下载音频失败: ${getErrorMessage(extractError)}`)
    }

    try {
      const stdout = await runYtDlp(buildDirectAudioArgs(url, outputPath), 1024 * 1024 * 10)
      return resolveDownloadedFile(stdout, outputDir, startTime)
    } catch (fallbackError) {
      throw new Error(
        `下载音频失败（检测到系统缺少 ffmpeg，且回退直下音频也失败）: ${getErrorMessage(
          fallbackError
        )}`
      )
    }
  }
}

export function downloadAudioWithProgress(
  url: string,
  outputDir: string,
  onProgress: (progress: number) => void
): Promise<string> {
  return new Promise((resolve, reject) => {
    const start = async () => {
      if (!fs.existsSync(outputDir)) {
        fs.mkdirSync(outputDir, { recursive: true })
      }

      const outputPath = path.join(outputDir, '%(id)s.%(ext)s')
      const startTime = Date.now()
      const runner = await resolveYtDlpRunner()

      const runDownload = async (args: string[]): Promise<{ code: number; stderr: string }> => {
        return new Promise((resolveDownload, rejectDownload) => {
          let stderr = ''

          const child = spawn(runner.command, [...runner.baseArgs, ...args], {
            windowsHide: true,
            shell: false,
          })

          const handleProgressOutput = (chunk: Buffer) => {
            const output = chunk.toString()
            const progressMatch = output.match(/(\d+\.?\d*)%/)
            if (progressMatch) {
              onProgress(parseFloat(progressMatch[1]))
            }
          }

          child.stdout?.on('data', handleProgressOutput)
          child.stderr?.on('data', (chunk: Buffer) => {
            stderr += chunk.toString()
            handleProgressOutput(chunk)
          })

          child.on('close', (code) => {
            resolveDownload({ code: code ?? -1, stderr })
          })

          child.on('error', rejectDownload)
        })
      }

      let result = await runDownload(buildExtractAudioArgs(url, outputPath, true))
      if (result.code !== 0 && isFfmpegMissing({ stderr: result.stderr })) {
        result = await runDownload(buildDirectAudioArgs(url, outputPath, true))
      }

      if (result.code === 0) {
        const audioFile = findNewestAudioFile(outputDir, startTime)
        if (audioFile) {
          resolve(audioFile)
          return
        }

        reject(new Error('Unable to locate the downloaded audio file'))
        return
      }

      reject(new Error(`下载失败，退出码: ${result.code}`))
    }

    start().catch((error) => reject(new Error(`下载音频失败: ${getErrorMessage(error)}`)))
  })
}
