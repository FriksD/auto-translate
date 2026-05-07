# Page Exit Processing Resume Design

## Context

The app processes a project in one page-level async flow:

1. download or accept an audio file
2. call the Python transcription endpoint
3. call the Python diarization endpoint
4. merge transcript and diarization segments
5. write the final transcript to the project

The Python service already exposes in-memory progress endpoints for transcription and diarization. The frontend currently loses useful progress because the processing state lives mostly in the mounted `ProjectPage` component and project updates are not consistently mirrored into the project list.

## Scope

This change handles leaving the processing page while the desktop app remains open. Closing the Electron app and resuming after a restart is out of scope.

## Goals

- A user can return to the project list or another page while transcription is running.
- Reopening the project shows the processing screen and resumes progress polling.
- Project metadata, audio path, current stage, and partial transcript are preserved in the in-memory project list.
- If transcription completed while the user was away, reopening the project can continue into diarization.
- If processing fails, the project keeps an error state instead of silently resetting to the video step.

## Non-Goals

- No persistent job recovery after app restart.
- No new global task queue.
- No cancellation support in this pass.
- No change to the Python transcription algorithm.

## Design

Add a `processingState` field to each project. It stores the phase, progress label, numeric progress, audio path, and error message. The project store updates both `currentProject` and `projects` whenever a project changes, so leaving the project page no longer loses the latest state.

The project page derives its initial step from `processingState` before falling back to `article`, `transcript`, or `video`. If a project is in a running processing phase, the page starts polling the appropriate Python progress endpoint when opened. The original async flow still performs the work when started from the page, but every important transition is saved to the project itself.

When the page unmounts, polling stops only for that page instance. It does not clear the project processing state or reset the project step. The Python request may still finish; if the page is gone before the final local merge happens, reopening the project uses the progress endpoint to detect completed transcription and continue from the saved audio path.

## Components

- `src/stores/projectProcessing.ts`
  - Defines processing phases and helper functions for deriving the visible step and checking whether work is active.
- `src/stores/appStore.ts`
  - Adds `processingState` to `Project`.
  - Updates project changes into both `currentProject` and `projects`.
- `src/pages/ProjectPage.tsx`
  - Persists processing transitions into `processingState`.
  - Restores the processing screen from project state.
  - Resumes transcription or diarization polling when a processing project is reopened.
- `tests/projectProcessing.test.cjs`
  - Covers step derivation and active processing rules.

## Error Handling

Errors are stored as `processingState.phase = "error"` with a user-facing message. Reopening the project shows the video step for retry, but the project retains the error state so the failure is inspectable in the project data.

## Verification

- `node --test tests/projectProcessing.test.cjs`
- `npm run build:vite`
