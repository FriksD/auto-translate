import * as http from 'node:http'

const DEFAULT_REQUEST_TIMEOUT_MS = 10 * 60 * 1000
const LONG_RUNNING_REQUEST_TIMEOUT_MS = 2 * 60 * 60 * 1000

export interface TranscriptionProgress {
  status: 'idle' | 'running' | 'completed' | 'error'
  stage: string
  progress: number
  message: string
  audio_path?: string | null
  processed_duration: number
  total_duration?: number | null
  segment_count: number
  language?: string | null
  language_probability?: number | null
  error?: string | null
  started_at?: number | null
  updated_at?: number | null
}

export interface DiarizationProgress {
  status: 'idle' | 'running' | 'completed' | 'error'
  stage: string
  progress: number
  message: string
  audio_path?: string | null
  processed_duration: number
  total_duration?: number | null
  chunk_count: number
  total_chunks: number
  error?: string | null
  started_at?: number | null
  updated_at?: number | null
}

export class PythonBridge {
  private baseUrl: string

  constructor(port: number) {
    this.baseUrl = `http://localhost:${port}`
  }

  private async request(endpoint: string, data: any, timeoutMs: number = DEFAULT_REQUEST_TIMEOUT_MS): Promise<any> {
    return new Promise((resolve, reject) => {
      const body = JSON.stringify(data)
      let settled = false

      const finishResolve = (value: any) => {
        if (settled) {
          return
        }
        settled = true
        clearTimeout(timeoutId)
        resolve(value)
      }

      const finishReject = (error: Error) => {
        if (settled) {
          return
        }
        settled = true
        clearTimeout(timeoutId)
        reject(error)
      }

      const request = http.request(
        `${this.baseUrl}${endpoint}`,
        {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            'Content-Length': Buffer.byteLength(body),
          },
        },
        (response) => {
          let raw = ''

          response.setEncoding('utf8')
          response.on('data', (chunk) => {
            raw += chunk
          })

          response.on('end', () => {
            const statusCode = response.statusCode ?? 500
            const statusMessage = response.statusMessage ?? 'Unknown Error'

            let payload: any = null
            if (raw) {
              try {
                payload = JSON.parse(raw)
              } catch {
                finishReject(new Error(`Python服务返回了无效JSON (${statusCode}): ${raw.slice(0, 200)}`))
                return
              }
            }

            if (statusCode < 200 || statusCode >= 300) {
              const detail = payload?.detail ? String(payload.detail) : statusMessage
              finishReject(new Error(`Python服务请求失败 (${statusCode}): ${detail}`))
              return
            }

            finishResolve(payload)
          })

          response.on('error', (error) => {
            finishReject(error)
          })
        }
      )

      const timeoutId = setTimeout(() => {
        request.destroy(new Error(`Python服务请求超时 (${Math.ceil(timeoutMs / 1000)}s)`))
      }, timeoutMs)

      request.on('error', (error) => {
        finishReject(error)
      })

      request.write(body)
      request.end()
    })
  }

  /**
   * 语音转录
   */
  async transcribe(audioPath: string): Promise<any> {
    return this.request('/api/transcribe', {
      audio_path: audioPath,
    }, LONG_RUNNING_REQUEST_TIMEOUT_MS)
  }

  /**
   * 说话人分离
   */
  async diarize(audioPath: string, numSpeakers?: number): Promise<any> {
    const payload: { audio_path: string; num_speakers?: number } = {
      audio_path: audioPath,
    }

    if (numSpeakers !== undefined) {
      payload.num_speakers = numSpeakers
    }

    return this.request('/api/diarize', payload, LONG_RUNNING_REQUEST_TIMEOUT_MS)
  }

  async getTranscriptionProgress(audioPath?: string): Promise<TranscriptionProgress> {
    return this.request('/api/transcribe/progress', {
      audio_path: audioPath,
    })
  }

  async getDiarizationProgress(audioPath?: string): Promise<DiarizationProgress> {
    return this.request('/api/diarize/progress', {
      audio_path: audioPath,
    })
  }

  /**
   * 翻译生成新闻稿
   */
  async translate(
    transcript: any,
    speakers: any,
    context: any,
    config: any
  ): Promise<string> {
    const result = await this.request('/api/translate', {
      transcript,
      speakers,
      context,
      llm_config: config,
    })
    return result.article
  }

  /**
   * 预览发送给LLM的提示词
   */
  async previewPrompt(transcript: any, speakers: any, context: any): Promise<string> {
    const result = await this.request('/api/prompt/preview', {
      transcript,
      speakers,
      context,
    })
    return result.prompt
  }

  /**
   * 获取HLTV信息
   */
  async getHLTVInfo(query: string, type: 'team' | 'player'): Promise<any> {
    return this.request('/api/hltv', {
      query,
      type,
    })
  }

  /**
   * 获取百炼模型列表
   */
  async getBailianModels(apiKey: string): Promise<any> {
    return this.request('/api/bailian/models', {
      api_key: apiKey,
    })
  }

  /**
   * 检查服务状态
   */
  async healthCheck(): Promise<boolean> {
    try {
      const response = await fetch(`${this.baseUrl}/health`)
      return response.ok
    } catch {
      return false
    }
  }
}
