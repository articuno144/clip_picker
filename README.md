# Clip Picker

A visual contact-sheet-based clip range selector for building Bilibili commentary / video essay projects. Replaces the tedious "guess timestamp → extract clip → check → re-extract" loop with a direct-manipulation GUI.

The UI defaults to English and includes a language selector for Chinese.

## What it does

For each segment of your narration script, the app shows a contact sheet grid for any source video in a folder. You click two cells to select a start→end time range, including ranges that span multiple pages. Selected ranges appear on the right with visual duration bars against your pre-recorded narration timing. When all segments are done, click "完成" to batch-extract every clip with ffmpeg.

## Quick start

```bash
pip install -r requirements.txt
python clip_picker.py
# Open http://localhost:8720
```

To use the standalone repo against an existing project folder:

```bash
python /path/to/clip-picker/clip_picker.py --config /path/to/project/segments.json
```

## Project structure

```
clip-picker/
  clip_picker.py        # Flask backend + extract API
  clip_picker.html      # Single-page frontend
  segments.json         # Segment definitions (narration, timings, search hints)
  selections.json       # Your saved clip selections (auto-generated)
  videos/               # Source videos; any .mp4/.mov/.mkv/.m4v/.webm files
  contact_sheets/       # Pre-generated JPG/PNG/WebP grids named for each video
  assets/shots/<id>/    # Extracted clips land here
    parts/              # Individual parts per segment
    clip.mp4            # Concatenated full segment clip
  script.md             # Original narration script (for reference)
```

## segments.json format

```json
{
  "title": "Project title",
  "episode_dir": "videos",
  "contact_sheet_dir": "contact_sheets",
  "output_dir": "assets/shots",
  "contact_sheet_cols": 8,
  "contact_sheet_rows": 8,
  "seconds_per_frame": 5,
  "segments": [
    {
      "id": "intro_title_card",
      "slug": "Intro",
      "narration": "Full narration text...",
      "suggested_episode": "12",
      "narration_duration": 24.1,
      "hook": "Short visual hook description",
      "search_hint": "What to look for in the contact sheet (used as gemini prompt)"
    }
  ]
}
```

`seconds_per_frame` is the fallback timing when no source duration is available. When source videos and contact sheets are present, the app derives each page's duration as `video_duration / number_of_contact_sheet_pages`, so it works with contact sheets generated at any consistent sampling density.

## Keyboard shortcuts

| Key | Action |
|---|---|
| `⇧N` | Next segment |
| `⇧P` | Previous segment |
| `⌥←` | Previous page |
| `⌥→` | Next page |
| `Esc` | Cancel current selection |

## Generating contact sheets

For a new project, generate contact sheets before running the app. Sheet filenames can use any of these prefixes:

- `ep01_page0.jpg` for a numeric video id like `01`
- `12_page0.jpg` for the same numeric video id
- `My_Video_page0.jpg` for a non-numeric video stem

```bash
# Example: fixed 8×8 pages, roughly 5 seconds per cell
mkdir -p contact_sheets
for file in videos/*.{mp4,mov,mkv,m4v,webm}; do
  [ -e "$file" ] || continue
  base=$(basename "$file")
  stem="${base%.*}"
  safe=$(printf '%s' "$stem" | tr -cs '[:alnum:]_.-' '_')
  dur=$(ffprobe -v error -show_entries format=duration \
    -of default=noprint_wrappers=1:nokey=1 "$file")
  pages=$(( (${dur%.*} + 319) / 320 ))
  for page in $(seq 0 $((pages - 1))); do
    ffmpeg -ss $((page * 320)) -i "$file" -t 320 \
      -vf "fps=1/5,scale=360:-1,tile=8x8:margin=2:padding=1:color=0x111111" \
      -frames:v 1 "contact_sheets/${safe}_page${page}.jpg" -y
  done
done
```

## Workflow

1. Write `script.md` (narration + frontmatter)
2. Put source videos in the folder configured by `episode_dir`
3. Edit `segments.json` with segment definitions, narration durations, and search hints
4. Generate contact sheets for all episodes
5. Run `python clip_picker.py` and visually select clip ranges
6. Choose an export type and click `导出`
   - `selections.json`: saves and downloads the current selections
   - `FCPXML`: saves selections, extracts clips, runs `prepare.py`, then downloads the generated FCPXML
7. Import the FCPXML into DaVinci Resolve for final editing

## FCPXML export

FCPXML export needs a `prepare.py` script. Clip Picker searches in this order:

1. `prepare_script` in `segments.json`
2. `prepare.py` in the same project folder as `segments.json`
3. `~/.claude/skills/bilibili-video/prepare.py`

By default the generated file is expected at `out/timeline.fcpxml`. Override it with `fcpxml_file` in `segments.json` if your prepare script writes somewhere else.

## Notes

- Episode files should be 720p SDR (H.264, BT.709) to avoid HDR tone-mapping issues
- Contact sheets are generated at `fps=1/5` (one frame every 5 seconds)
- The app auto-saves selections to `selections.json` on every change
- Clip extraction uses `-c copy` (no re-encode), so it's fast and lossless
- For multi-part segments, parts are extracted individually and also concatenated into `clip.mp4`
