/**
 * 下载 yt-dlp.exe 和 ffmpeg.exe 到 resources/bin/。
 * 这些 exe 会被 electron-builder 打包进安装包，确保新环境无需安装依赖。
 *
 * yt-dlp: https://github.com/yt-dlp/yt-dlp/releases
 * ffmpeg: https://www.gyan.dev/ffmpeg/builds/ (Windows)
 *
 * 用法: node scripts/download-binaries.js
 */
const { execSync } = require('child_process')
const path = require('path')
const fs = require('fs')
const https = require('https')
const { createWriteStream } = require('fs')
const { pipeline } = require('stream/promises')

const binDir = path.resolve(__dirname, '..', 'resources', 'bin')

if (!fs.existsSync(binDir)) {
  fs.mkdirSync(binDir, { recursive: true })
}

function downloadFile(url, destPath) {
  return new Promise((resolve, reject) => {
    console.log(`Downloading ${url} -> ${destPath}`)
    https.get(url, (response) => {
      if (response.statusCode === 302 || response.statusCode === 301) {
        https.get(response.headers.location, (redirectRes) => {
          const file = createWriteStream(destPath)
          pipeline(redirectRes, file).then(resolve).catch(reject)
        })
        return
      }

      if (response.statusCode !== 200) {
        reject(new Error(`HTTP ${response.statusCode}`))
        return
      }

      const file = createWriteStream(destPath)
      pipeline(response, file).then(resolve).catch(reject)
    }).on('error', reject)
  })
}

async function main() {
  // 下载 yt-dlp.exe
  const ytDlpUrl = 'https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp.exe'
  const ytDlpPath = path.join(binDir, 'yt-dlp.exe')

  if (!fs.existsSync(ytDlpPath)) {
    console.log('Downloading yt-dlp.exe...')
    try {
      await downloadFile(ytDlpUrl, ytDlpPath)
      console.log('yt-dlp.exe downloaded successfully')
    } catch (err) {
      console.error('Failed to download yt-dlp.exe:', err.message)
      console.log('Please download manually from: https://github.com/yt-dlp/yt-dlp/releases')
    }
  } else {
    console.log('yt-dlp.exe already exists, skipping')
  }

  // ffmpeg 需要手动下载（太大且需要解压）
  // 用户可从 https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip 下载
  // 将 ffmpeg.exe 和 ffprobe.exe 放入 resources/bin/
  const ffmpegPath = path.join(binDir, 'ffmpeg.exe')
  const ffprobePath = path.join(binDir, 'ffprobe.exe')

  if (!fs.existsSync(ffmpegPath) || !fs.existsSync(ffprobePath)) {
    console.log('\nffmpeg.exe / ffprobe.exe not found in resources/bin/')
    console.log('Download ffmpeg from: https://www.gyan.dev/ffmpeg/builds/')
    console.log('Extract ffmpeg.exe and ffprobe.exe into resources/bin/')
  } else {
    console.log('ffmpeg.exe and ffprobe.exe found')
  }
}

main().catch(console.error)
