const assert = require('node:assert/strict')
const test = require('node:test')
const fs = require('node:fs')
const path = require('node:path')
const ts = require('typescript')
const vm = require('node:vm')

function loadModule(relativePath) {
  const filename = path.resolve(__dirname, '..', relativePath)
  const source = fs.readFileSync(filename, 'utf8')
  const compiled = ts.transpileModule(source, {
    compilerOptions: {
      module: ts.ModuleKind.CommonJS,
      target: ts.ScriptTarget.ES2020,
    },
  }).outputText
  const module = { exports: {} }
  const context = {
    module,
    exports: module.exports,
    require,
    __dirname: path.dirname(filename),
    __filename: filename,
  }
  vm.runInNewContext(compiled, context, { filename })
  return module.exports
}

const {
  applyProjectUpdateById,
  applyProjectUpdate,
  getProjectStep,
  isProcessingPhaseActive,
} = loadModule('src/stores/projectProcessing.ts')

test('running transcription projects reopen on processing step', () => {
  const project = {
    id: 'p1',
    name: 'Interview',
    videoUrl: 'https://example.com/video',
    speakers: {},
    createdAt: new Date(),
    updatedAt: new Date(),
    processingState: {
      phase: 'transcribing',
      message: 'Transcribing...',
      progress: 40,
      audioPath: 'C:/audio/interview.m4a',
      updatedAt: Date.now(),
    },
  }

  assert.equal(getProjectStep(project), 'processing')
  assert.equal(isProcessingPhaseActive(project.processingState.phase), true)
})

test('completed transcript projects reopen on speaker labeling step', () => {
  const project = {
    id: 'p2',
    name: 'Interview',
    videoUrl: '',
    speakers: {},
    transcript: [{ start: 0, end: 1, text: 'hello' }],
    createdAt: new Date(),
    updatedAt: new Date(),
  }

  assert.equal(getProjectStep(project), 'speakers')
})

test('failed processing projects reopen on video step for retry', () => {
  const project = {
    id: 'p3',
    name: 'Interview',
    videoUrl: '',
    speakers: {},
    createdAt: new Date(),
    updatedAt: new Date(),
    processingState: {
      phase: 'error',
      message: 'Transcription failed',
      progress: 42,
      audioPath: 'C:/audio/interview.m4a',
      error: 'boom',
      updatedAt: Date.now(),
    },
  }

  assert.equal(getProjectStep(project), 'video')
  assert.equal(isProcessingPhaseActive(project.processingState.phase), false)
})

test('project updates are mirrored into the project list', () => {
  const currentProject = {
    id: 'p1',
    name: 'Interview',
    videoUrl: '',
    speakers: {},
    createdAt: new Date('2026-04-29T00:00:00Z'),
    updatedAt: new Date('2026-04-29T00:00:00Z'),
  }
  const otherProject = {
    id: 'p2',
    name: 'Other',
    videoUrl: '',
    speakers: {},
    createdAt: new Date('2026-04-29T00:00:00Z'),
    updatedAt: new Date('2026-04-29T00:00:00Z'),
  }

  const result = applyProjectUpdate([currentProject, otherProject], currentProject, {
    processingState: {
      phase: 'transcribing',
      message: 'Transcribing...',
      progress: 35,
      audioPath: 'C:/audio/interview.m4a',
      updatedAt: Date.now(),
    },
  })

  assert.equal(result.currentProject.processingState.phase, 'transcribing')
  assert.equal(result.projects[0].processingState.phase, 'transcribing')
  assert.equal(result.projects[1], otherProject)
})

test('project updates can target a project after the page unmounts', () => {
  const project = {
    id: 'p1',
    name: 'Interview',
    videoUrl: '',
    speakers: {},
    createdAt: new Date('2026-04-29T00:00:00Z'),
    updatedAt: new Date('2026-04-29T00:00:00Z'),
  }

  const result = applyProjectUpdateById([project], null, 'p1', {
    processingState: {
      phase: 'diarizing',
      message: 'Diarizing...',
      progress: 92,
      audioPath: 'C:/audio/interview.m4a',
      updatedAt: Date.now(),
    },
  })

  assert.equal(result.currentProject, null)
  assert.equal(result.projects[0].processingState.phase, 'diarizing')
})
