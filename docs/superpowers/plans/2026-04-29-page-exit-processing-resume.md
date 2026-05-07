# Page Exit Processing Resume Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Keep project processing state available when the user leaves and later reopens the project page during an in-app transcription flow.

**Architecture:** Store processing state on the project itself and keep the project list synchronized with current project updates. Extract pure processing-state helpers so restore rules can be tested without rendering React. Let `ProjectPage` restore the processing view and continue from transcription completion into diarization when possible.

**Tech Stack:** React, TypeScript, Zustand, Electron IPC, FastAPI progress endpoints, Node built-in test runner.

---

## File Structure

- Create `src/stores/projectProcessing.ts` for reusable processing phase types and step derivation helpers.
- Modify `src/stores/appStore.ts` so `Project` includes `processingState` and `updateProject` writes through to `projects`.
- Modify `src/pages/ProjectPage.tsx` so processing transitions are persisted and reopened projects resume visible progress.
- Modify `src/types/electron.d.ts` so renderer typings include diarization progress.
- Create `tests/projectProcessing.test.cjs` to test processing-state helpers using Node and the local TypeScript compiler.

### Task 1: Processing State Helpers

**Files:**
- Create: `tests/projectProcessing.test.cjs`
- Create: `src/stores/projectProcessing.ts`

- [ ] **Step 1: Write the failing test**

Create `tests/projectProcessing.test.cjs` with tests that load `src/stores/projectProcessing.ts` through `typescript.transpileModule` and assert:

```js
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
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `node --test tests/projectProcessing.test.cjs`

Expected: FAIL because `src/stores/projectProcessing.ts` does not exist.

- [ ] **Step 3: Write minimal helper implementation**

Create `src/stores/projectProcessing.ts` with exported phase types, `isProcessingPhaseActive`, and `getProjectStep`.

- [ ] **Step 4: Run the test to verify it passes**

Run: `node --test tests/projectProcessing.test.cjs`

Expected: PASS.

### Task 2: Store Synchronization

**Files:**
- Modify: `src/stores/appStore.ts`
- Test: `npm run build:vite`

- [ ] **Step 1: Add project processing state types**

Import processing types from `projectProcessing.ts` and add optional `processingState` to `Project`.

- [ ] **Step 2: Update `updateProject` write-through behavior**

Change `updateProject` so it updates `currentProject` and replaces the same project in `projects` if present.

- [ ] **Step 3: Type-check through Vite build**

Run: `npm run build:vite`

Expected: any errors point to callers needing updated types.

### Task 3: Project Page Resume Flow

**Files:**
- Modify: `src/pages/ProjectPage.tsx`
- Modify: `src/types/electron.d.ts`
- Test: `npm run build:vite`

- [ ] **Step 1: Use `getProjectStep` when opening projects**

Replace local article/transcript/video step branching with the helper.

- [ ] **Step 2: Persist processing transitions**

Before and after download, transcription, diarization, success, and failure, call `updateProject({ processingState: ... })` with the current phase, message, progress, audio path, and error when present.

- [ ] **Step 3: Resume polling on reopen**

Add an effect that starts transcription polling when `processingState.phase === "transcribing"` and diarization polling when `processingState.phase === "diarizing"`.

- [ ] **Step 4: Continue after completed transcription**

If reopened progress reports transcription completed and no transcript exists yet, run diarization from the saved audio path and merge once both calls return.

- [ ] **Step 5: Build**

Run: `npm run build:vite`

Expected: PASS.

### Task 4: Final Verification

**Files:**
- No additional source files.

- [ ] **Step 1: Run helper tests**

Run: `node --test tests/projectProcessing.test.cjs`

Expected: PASS.

- [ ] **Step 2: Run production build**

Run: `npm run build:vite`

Expected: PASS.
