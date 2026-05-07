/**
 * 使用 PyInstaller 将 Python FastAPI 后端打包为独立 exe。
 * 输出到 resources/backend/，由 electron-builder 打包进安装包。
 *
 * 用法: node scripts/build-backend.js
 * 需要先安装 PyInstaller: pip install pyinstaller
 */
const { execSync } = require('child_process')
const path = require('path')
const fs = require('fs')

const projectRoot = path.resolve(__dirname, '..')
const pythonDir = path.join(projectRoot, 'python')
const outputDir = path.join(projectRoot, 'resources')

if (!fs.existsSync(outputDir)) {
  fs.mkdirSync(outputDir, { recursive: true })
}

// 只导入 PyInstaller 自动检测可能遗漏的模块
const hiddenImports = [
  'faster_whisper',
  'pyannote.audio',
  'pyannote.core',
  'pyannote.metrics',
  'curl_cffi',
  'hltv_async_api',
  'dotenv',
  'bs4',
  'lxml',
  // FastAPI 及依赖（PyInstaller 经常漏掉）
  'fastapi',
  'uvicorn',
  'uvicorn.loops',
  'uvicorn.loops.auto',
  'uvicorn.protocols',
  'uvicorn.protocols.http',
  'uvicorn.protocols.http.auto',
  'uvicorn.protocols.websockets',
  'uvicorn.protocols.websockets.auto',
  'uvicorn.lifespan',
  'uvicorn.lifespan.on',
  'starlette',
  'starlette.middleware',
  'httpx',
  'python_multipart',
  'openai',
  'anthropic',
  'requests',
  'yt_dlp',
  'aiohttp',
  'websockets',
  'certifi',
  'multipart',
]

// 需要深度递归收集子模块的包
const collectSubmodules = [
  'torch',
  'pyannote.audio',
  'faster_whisper',
]

// 排除无用但大型的传递依赖（项目完全不使用）
const excludeModules = [
  'tensorflow',       // 1.1GB，pyannote 的错误传递依赖
  'tensorboard',      // TensorFlow 的附属
  'cv2',              // 99MB，OpenCV，未使用
  'pyarrow',          // 77MB，Apache Arrow，未使用
  'onnxruntime',      // 34MB，ONNX Runtime，未使用
  'matplotlib',       // 11MB，绘图库，后端不需要
  'pandas',           // 18MB，数据帧库，未直接使用
  'jupyter',          // Jupyter notebook，未使用
  'ipython',          // 交互式 Python，未使用
  'notebook',         // Jupyter notebook，未使用
  'sqlalchemy',       // ORM，未使用
  'PIL',              // Pillow 图片库，未使用
  'sympy',            // 符号数学，未使用
]

const hiddenImportArgs = hiddenImports.flatMap((m) => ['--hidden-import', m])
const collectSubmoduleArgs = collectSubmodules.flatMap((m) => ['--collect-submodules', m])
const excludeModuleArgs = excludeModules.flatMap((m) => ['--exclude-module', m])

const pyinstallerArgs = [
  '--onedir',
  '--clean',
  '--noconfirm',
  '--name', 'backend',
  '--distpath', outputDir,
  '--workpath', path.join(projectRoot, 'build', 'pyinstaller'),
  '--specpath', path.join(projectRoot, 'build'),
  '--runtime-hook', path.join(projectRoot, 'build', 'hook-torch-dll.py'),
  '--paths', pythonDir,
  '--add-data', `${pythonDir}${path.sep}services${path.sep}__init__.py${path.delimiter}services${path.sep}`,
  '--add-data', `${pythonDir}${path.sep}services${path.sep}whisper_service.py${path.delimiter}services${path.sep}`,
  '--add-data', `${pythonDir}${path.sep}services${path.sep}llm_service.py${path.delimiter}services${path.sep}`,
  '--add-data', `${pythonDir}${path.sep}services${path.sep}hltv_scraper.py${path.delimiter}services${path.sep}`,
  '--add-data', `${pythonDir}${path.sep}services${path.sep}diarization.py${path.delimiter}services${path.sep}`,
  ...hiddenImportArgs,
  ...collectSubmoduleArgs,
  ...excludeModuleArgs,
  path.join(pythonDir, 'server.py'),
]

console.log('Running PyInstaller...')
console.log(`Command: pyinstaller ${pyinstallerArgs.join(' ')}`)

try {
  execSync(`pyinstaller ${pyinstallerArgs.join(' ')}`, {
    cwd: projectRoot,
    stdio: 'inherit',
    env: { ...process.env, PYTHONIOENCODING: 'utf-8' },
  })
  console.log('\nBackend built successfully to resources/backend/')
} catch (err) {
  console.error('PyInstaller build failed:', err.message)
  process.exit(1)
}
