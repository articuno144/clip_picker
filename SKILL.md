---
name: clip-picker
description: Use the standalone Clip Picker UI to visually select video clip ranges from contact sheets, save selections, extract clips, and optionally export FCPXML for editing workflows.
---

# Clip Picker

Use this tool when a project has source videos, contact sheets, and segment metadata, and the user wants visual clip selection instead of manually guessing timestamps.

## Run

```bash
cd /path/to/clip-picker
pip install -r requirements.txt
python clip_picker.py --config /path/to/project/segments.json
```

Open `http://127.0.0.1:8720/`. Do not open `clip_picker.html` directly with `file://`, because export and progress require the Flask API.

## Generate contact sheets

```bash
python clip_picker.py generate-sheets --config /path/to/project/segments.json
```

This uses `contact_sheet_cols`, `contact_sheet_rows`, and `seconds_per_frame` from `segments.json`. The UI uses the same timing model, and cells beyond the actual video duration are invalid.

## Project config

The target project needs a `segments.json` with:

- `episode_dir`: folder containing source videos
- `contact_sheet_dir`: folder containing contact sheet images
- `subtitle_dir`: optional folder containing SRT files
- `output_dir`: where extracted clips should be written
- `segments`: segment list with `id`, `slug`, `narration`, `suggested_episode`, and `narration_duration`

Contact sheets should be named with one of these patterns:

- `ep01_page0.jpg`
- `01_page0.jpg`
- `<safe_video_stem>_page0.jpg`

Prefer a 720p SDR source folder, such as `episodes_720p`, when the project also has higher-resolution or HDR copies.

## Workflow

1. Start the server with `--config`.
2. Use the language selector if needed; English is the default UI language.
3. Select a segment and video.
4. Click a start cell, navigate pages if needed, then click an end cell.
5. Hover over cells to inspect nearby SRT subtitles when available; previous/next cell subtitles are greyed and current-cell subtitles are emphasized.
6. Add as many parts as the segment needs.
7. Choose export type:
   - `selections.json`: saves and downloads selections.
   - `FCPXML`: saves selections, extracts clips, runs `prepare.py`, then downloads the FCPXML.

## FCPXML notes

FCPXML export requires a `prepare.py` script. Clip Picker searches:

1. `prepare_script` in `segments.json`
2. `prepare.py` next to `segments.json`
3. `~/.claude/skills/bilibili-video/prepare.py`

If the generated FCPXML is not `out/timeline.fcpxml`, set `fcpxml_file` in `segments.json`.
