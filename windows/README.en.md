# 懒得笔记 (Video2Obsidian) — Turn Local Videos into Obsidian Notes (Windows 11)

> Drop videos into a folder and get text notes as Markdown, all on your local Windows PC.

[简体中文](./README.md) | English

## What is this?

懒得笔记 (Video2Obsidian) watches a video folder on your PC, transcribes new videos locally on your NVIDIA GPU, fixes known wrong words, splits the text into readable paragraphs, and saves the result as Markdown.

If you provide an Obsidian vault directory, notes are written there mirroring the video folder structure. If you leave it empty, notes stay in the data directory and your existing vault is untouched. The whole process is **fully offline**: no cloud API calls, no API cost, and it never downloads models behind your back.

## Key features

- **Watch-and-process**: fill in a video folder, press start, then drop files in. Jobs queue in the background; you do not need to keep watching the page.
- **Local GPU transcription**: uses `faster-whisper` (CTranslate2 backend, NVIDIA CUDA) in your local Python environment, with the bundled `ffmpeg` extracting audio first.
- **Word fixes**: maintain “wrong word → correct word” pairs (names, terms) in the console. New transcripts apply them automatically; existing results can be re-run.
- **Draft and publish**: transcripts become paragraph-style Markdown. With a vault directory set, files mirror into Obsidian; without it, the run stops at render (a deliberate `RENDER_ONLY` outcome).
- **Visible jobs with retry**: the job list shows Discover → Transcribe → Tidy → Draft → Publish progress. Failed jobs can be retried, previewed, and opened in Explorer or Obsidian.
- **Hard safety rules**: existing notes are never overwritten (No-Clobber); when the GPU is unavailable it fails loudly instead of silently falling back to CPU; offline-only, no sneaky downloads.

## Who is it for?

- Obsidian users with course recordings, meeting recordings, or interview footage to turn into text.
- People who prefer to keep video and text on their own machine instead of uploading to a cloud service.
- Users on Windows 11 with an NVIDIA GPU who can follow the setup steps below.

## Quick start

Double-click **`start.bat`** in the repository root (or run `.\start.ps1` in PowerShell). The script:

1. creates `.venv` with official Python 3.12 if missing;
2. installs pinned dependencies from `requirements.txt`;
3. checks port usage (reports only — it never kills another process);
4. starts the local console at:

```text
http://127.0.0.1:8899/
```

Then, in order:

1. Fill in the **video folder** (required, absolute local path).
2. Fill in the **vault directory** (optional; transcription still works when empty).
3. Press **Start watching**, then copy video files into the video folder.

Click a row in the job list to read the finished note.

## Installation

### Requirements

- Windows 11 (64-bit).
- Official **Python 3.12** (`py -3.12` must work; `start.ps1` uses it to create `.venv`).
- **NVIDIA GPU + CUDA runtime**: drivers installed, `nvidia-smi` shows the card. If the GPU is unavailable the program stops with a clear error — it **never silently falls back to CPU**; if you truly need CPU, explicitly enable “CPU int8 fallback” in the page (much slower).
- **Bundled ffmpeg**: the delivery package ships `third_party\ffmpeg\bin\ffmpeg.exe`. If absent, set the `V2O_FFMPEG` environment variable to an absolute path, or let an `ffmpeg` on PATH be used as a fallback (the log honestly records which one ran).
- An Obsidian vault directory is optional.

### Model freeze (once, on a networked machine)

The app is **offline-only** and never downloads models. Prepare the CT2 model on a networked machine first:

1. Download the CTranslate2 checkpoint of `Systran/faster-whisper-large-v3-turbo` manually; record its source URL and revision (commit).
2. Copy the model directory to this machine (e.g. `D:\models\faster-whisper-large-v3-turbo`).
3. Generate the manifest (SHA-256 per file included) inside the venv:

```powershell
.\.venv\Scripts\python.exe tools\freeze_model_manifest.py ^
    --model-dir D:\models\faster-whisper-large-v3-turbo ^
    --revision <checkpoint commit> ^
    --source https://huggingface.co/Systran/faster-whisper-large-v3-turbo ^
    --license MIT --license-file LICENSE
```

The manifest lands in `models\MODEL_MANIFEST.json`. Before listening starts, every file's SHA-256 is verified; any mismatch stops transcription with a clear error.

### How startup works

`start.ps1` behavior (`start.bat` is a thin wrapper):

- venv: `.venv\Scripts\python.exe` (auto-created via `py -3.12` when missing).
- Dependencies: pinned install from `requirements.txt`.
- Without `faster_whisper` / `ctranslate2`: the console still opens, but starting a job returns `PRECHECK_ASR_BACKEND_MISSING`. Install the pinned dependencies and restart.
- The port defaults to `127.0.0.1:8899` and only listens locally; override with `$env:V2O_PORT=8900; .\start.ps1`. When the port is taken the script exits with a hint — it never kills the occupying process or binds 0.0.0.0.
- Default data directory: `%LOCALAPPDATA%\Video2Obsidian\data`.

## Usage

### Minimal path

```powershell
.\start.ps1
# Open http://127.0.0.1:8899/
# Fill video folder -> Start watching -> Drop videos in -> Check the job list
```

Accepted input suffixes (as watched): `.mp4` `.mov` `.mkv` `.m4v` `.avi` `.webm`.

### Word fixes

Add one “wrong → correct” pair in the vocabulary area to affect new transcripts. Re-run existing results to apply updated pairs.

## Configuration

| Field in the page | Required | Notes |
| --- | --- | --- |
| Video folder | Yes | Absolute local path to watch. Pasted quoted paths have quotes stripped automatically. |
| Vault directory | No | When empty, notes stay in the data directory and Obsidian is untouched. |
| Data directory | No | Advanced. Defaults to `%LOCALAPPDATA%\Video2Obsidian\data`. |

## Delivery & acceptance docs

- [Windows handoff & on-device acceptance checklist](./docs/WINDOWS-HANDOFF.md): the list of items that still require a real Windows machine, acceptance commands, and hard rules.

## Known limitations

- Target device is Windows 11 + NVIDIA CUDA. Other platforms (macOS/Linux included) are not verified in this repository.
- Without `faster_whisper` / `ctranslate2`, without the CUDA runtime, or with an unfrozen model manifest, transcription is unavailable and the console says so explicitly; it never silently downgrades to CPU or silently downloads anything.
- Watch state is lost on restart; press start again in the page after the service restarts. On restart, finished jobs are skipped and half-finished jobs are re-run automatically — no duplicate notes.
- **A video dropped into the watched folder is only picked up after it stops changing (about 7 seconds of quiet writes).** If a copy/download pauses longer than ~7 seconds and then continues, the run is first held back as “source still changing” and re-queued once writing finishes; in rare cases a partial transcript may be produced first, while the complete version is blocked by No-Clobber protection (existing notes are never overwritten). In that case, delete the partial `.md` and drop the video again.
- Real-GPU transcription, Defender/UAC prompts, the `obsidian://` protocol, and the long-path registry policy are **not yet verified on a real Windows machine**; see the itemized list in [WINDOWS-HANDOFF](./docs/WINDOWS-HANDOFF.md).
- There is no License file in the repository. Treat it as all rights reserved until a license is added.

## License

No License file is provided yet. Add one before public distribution and link it here.
