#!/usr/bin/env python3
"""Clip Picker — visual contact-sheet-based clip range selector for video essays.

Reads segments.json (segment definitions + narration timings) and presents an
8×8 contact sheet browser per segment.  The user drag-selects start→end frame
ranges on the grid, adds multiple parts, and sees real-time duration vs.
narration comparison.  Selections are saved to selections.json for downstream
extraction / FCPXML generation.

Run:  python clip_picker.py          # opens http://localhost:8720
"""

import json
import os
import re
import subprocess
import argparse
import sys
import threading
import time
import uuid
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parent

# ── config ──────────────────────────────────────────────────────────
PROJECT_ROOT = APP_ROOT
CFG_FILE = Path(os.environ.get("CLIP_PICKER_CONFIG", PROJECT_ROOT / "segments.json")).expanduser()
if not CFG_FILE.is_absolute():
    CFG_FILE = (Path.cwd() / CFG_FILE).resolve()
PROJECT_ROOT = CFG_FILE.parent
SEL_FILE = Path(os.environ.get("CLIP_PICKER_SELECTIONS", PROJECT_ROOT / "selections.json")).expanduser()
if not SEL_FILE.is_absolute():
    SEL_FILE = (PROJECT_ROOT / SEL_FILE).resolve()
HOST = "127.0.0.1"
PORT = 8720
DEFAULT_SHEET_COLS = 8
DEFAULT_SHEET_ROWS = 8
DEFAULT_SECS_PER_CELL = 5
VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".m4v", ".webm"}
EXPORT_JOBS = {}
JOB_LOCK = threading.Lock()

def configure_paths(config_path: str | None = None, selections_path: str | None = None) -> None:
    global CFG_FILE, SEL_FILE, PROJECT_ROOT
    if config_path:
        cfg_file = Path(config_path).expanduser()
        CFG_FILE = cfg_file if cfg_file.is_absolute() else (Path.cwd() / cfg_file).resolve()
        PROJECT_ROOT = CFG_FILE.parent
    if selections_path:
        sel_file = Path(selections_path).expanduser()
        SEL_FILE = sel_file if sel_file.is_absolute() else (PROJECT_ROOT / sel_file).resolve()
    else:
        SEL_FILE = PROJECT_ROOT / "selections.json"

# ── helpers ─────────────────────────────────────────────────────────
def load_cfg() -> dict:
    with open(CFG_FILE, encoding="utf-8") as f:
        cfg = json.load(f)
    if not isinstance(cfg.get("segments"), list):
        raise ValueError("segments.json must contain a 'segments' list")
    return cfg

def cfg_path(cfg: dict, key: str, default: Path) -> Path:
    value = cfg.get(key)
    if not value:
        return default
    path = Path(value).expanduser()
    return path if path.is_absolute() else PROJECT_ROOT / path

def runtime_settings(cfg: dict) -> dict:
    cols = int(cfg.get("contact_sheet_cols", DEFAULT_SHEET_COLS))
    rows = int(cfg.get("contact_sheet_rows", DEFAULT_SHEET_ROWS))
    secs = float(cfg.get("seconds_per_frame", DEFAULT_SECS_PER_CELL))
    if cols <= 0 or rows <= 0 or secs <= 0:
        raise ValueError("contact sheet cols/rows/seconds_per_frame must be positive")
    return {
        "episode_dir": cfg_path(cfg, "episode_dir", PROJECT_ROOT / "videos"),
        "sheet_dir": cfg_path(cfg, "contact_sheet_dir", PROJECT_ROOT / "contact_sheets"),
        "subtitle_dir": cfg_path(cfg, "subtitle_dir", PROJECT_ROOT / "subs"),
        "output_dir": cfg_path(cfg, "output_dir", PROJECT_ROOT / "assets" / "shots"),
        "cols": cols,
        "rows": rows,
        "secs": secs,
        "page_secs": cols * rows * secs,
    }

def save_selections(data: dict) -> None:
    if not isinstance(data, dict):
        raise ValueError("Selections payload must be a JSON object")
    with open(SEL_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

def load_selections() -> dict:
    if SEL_FILE.exists():
        with open(SEL_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    return {}

def episode_id_for_file(path: Path) -> str:
    m = re.match(r"^(\d{1,3})(?:\D|$)", path.name)
    if m:
        return m.group(1).zfill(2)
    return safe_label(path.stem)

def discover_episode_records(episode_dir: Path, cfg: dict) -> list[dict]:
    """Return stable episode records for every video in the configured folder."""
    records = []
    seen = set()
    if episode_dir.exists():
        for f in sorted(episode_dir.iterdir(), key=lambda p: p.name.lower()):
            if not f.is_file() or f.suffix.lower() not in VIDEO_EXTS:
                continue
            base_id = episode_id_for_file(f)
            ep_id = base_id
            suffix = 2
            while ep_id in seen:
                ep_id = f"{base_id}-{suffix}"
                suffix += 1
            seen.add(ep_id)
            records.append({"id": ep_id, "title": f.stem, "file": str(f)})

    preferred = [str(ep).zfill(2) if str(ep).isdigit() else str(ep) for ep in cfg.get("episodes", [])]
    order = {ep_id: i for i, ep_id in enumerate(preferred)}
    records.sort(key=lambda item: (order.get(item["id"], len(order)), item["id"].lower()))
    return records

def discover_sheets(sheet_dir: Path, episode: dict) -> dict:
    """Return {page_num: filename} for an episode."""
    ep_id = episode["id"]
    stem = safe_label(Path(episode["file"]).stem)
    prefix_groups = [[f"ep{ep_id}_page", f"{ep_id}_page"], [f"{stem}_page"]]
    if not sheet_dir.exists():
        return {}

    for prefixes in prefix_groups:
        sheets = {}
        for f in sorted(sheet_dir.iterdir(), key=lambda p: p.name.lower()):
            name = f.name
            if not f.is_file() or not name.lower().endswith((".jpg", ".jpeg", ".png", ".webp")):
                continue
            for prefix in prefixes:
                if name.startswith(prefix):
                    try:
                        page = int(Path(name[len(prefix):]).stem)
                        sheets[page] = name
                    except ValueError:
                        pass
                    break
        if sheets:
            return sheets
    return {}

def parse_subtitle_time(value: str) -> float:
    m = re.match(r"(\d+):(\d+):(\d+)[,.](\d+)", value.strip())
    if not m:
        raise ValueError(f"Invalid subtitle timestamp: {value}")
    hours, minutes, seconds, millis = m.groups()
    return int(hours) * 3600 + int(minutes) * 60 + int(seconds) + int(millis[:3].ljust(3, "0")) / 1000

def parse_srt(path: Path) -> list[dict]:
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    cues = []
    blocks = re.split(r"\n\s*\n", text.replace("\r\n", "\n").replace("\r", "\n").strip())
    for block in blocks:
        lines = [line.strip() for line in block.split("\n") if line.strip()]
        if not lines:
            continue
        time_line_idx = next((i for i, line in enumerate(lines) if "-->" in line), None)
        if time_line_idx is None:
            continue
        start_raw, end_raw = [part.strip().split()[0] for part in lines[time_line_idx].split("-->", 1)]
        cue_text = " ".join(lines[time_line_idx + 1:]).strip()
        if not cue_text:
            continue
        try:
            clean_text = re.sub(r"<[^>]+>", "", cue_text)
            clean_text = re.sub(r"\{\\[^}]+\}", "", clean_text).strip()
            cues.append({
                "start": parse_subtitle_time(start_raw),
                "end": parse_subtitle_time(end_raw),
                "text": clean_text,
            })
        except ValueError:
            continue
    return cues

def discover_subtitle_file(cfg: dict, settings: dict, episode: dict) -> Path | None:
    configured = cfg.get("subtitle_file")
    if isinstance(configured, dict):
        value = configured.get(episode["id"])
        if value:
            path = Path(value).expanduser()
            return path if path.is_absolute() else PROJECT_ROOT / path
    elif isinstance(configured, str):
        path = Path(configured).expanduser()
        return path if path.is_absolute() else PROJECT_ROOT / path

    subtitle_dir = settings["subtitle_dir"]
    ep_id = episode["id"]
    stem = safe_label(Path(episode["file"]).stem)
    prefixes = [f"ep{ep_id}", ep_id, stem]
    if subtitle_dir.exists():
        for f in sorted(subtitle_dir.iterdir(), key=lambda p: p.name.lower()):
            if not f.is_file() or f.suffix.lower() != ".srt":
                continue
            lower_stem = f.stem.lower()
            if any(lower_stem == prefix.lower() or lower_stem.startswith((prefix + "_").lower()) or lower_stem.startswith((prefix + "-").lower()) for prefix in prefixes):
                return f
    return None

def episode_subtitles(cfg: dict, settings: dict, episode: dict) -> list[dict]:
    subtitle_file = discover_subtitle_file(cfg, settings, episode)
    if not subtitle_file or not subtitle_file.exists():
        return []
    return parse_srt(subtitle_file)

def find_episode_record(episode_dir: Path, cfg: dict, ep: str):
    ep_norm = str(ep).zfill(2) if str(ep).isdigit() else str(ep)
    for record in discover_episode_records(episode_dir, cfg):
        if record["id"] == ep_norm:
            return record
    return None

def find_episode_file(episode_dir: Path, cfg: dict, ep: str):
    record = find_episode_record(episode_dir, cfg, ep)
    if not record:
        return None
    return Path(record["file"])

def get_episode_duration(file_path: Path) -> float:
    if not file_path:
        return 0
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", str(file_path)],
            capture_output=True, text=True, timeout=15, check=True)
        return float(json.loads(r.stdout)["format"]["duration"])
    except Exception:
        return 0

def safe_label(label: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", str(label or "part")).strip("._")
    return cleaned or "part"

def concat_file_line(path: Path) -> str:
    escaped = str(path.resolve()).replace("'", "'\\''")
    return f"file '{escaped}'\n"

def coerce_part(part: dict) -> tuple[float, float] | None:
    try:
        start_s = float(part["start"])
        end_s = float(part["end"])
    except (KeyError, TypeError, ValueError):
        return None
    if start_s < 0 or end_s <= start_s:
        return None
    return start_s, end_s

def page_count_from_sheets_and_duration(sheets: dict, duration: float, page_secs: float) -> int:
    from_duration = int((duration + page_secs - 0.001) // page_secs) if duration > 0 else 0
    from_sheets = (max(sheets) + 1) if sheets else 0
    return max(1, from_duration or from_sheets)

def episode_timing(sheets: dict, duration: float, settings: dict) -> dict:
    pages = page_count_from_sheets_and_duration(sheets, duration, settings["page_secs"])
    page_secs = settings["page_secs"]
    secs_per_cell = page_secs / (settings["cols"] * settings["rows"])
    return {
        "pages": pages,
        "page_secs": page_secs,
        "secs_per_cell": secs_per_cell,
    }

def cell_to_time(page: int, row: int, col: int, cols: int, rows: int, secs_per_cell: float) -> float:
    """Convert (page, row, col) → episode seconds."""
    return (page * rows * cols + row * cols + col) * secs_per_cell

def time_to_cell(t: float, cols: int, rows: int, secs_per_cell: float):
    """Convert episode seconds → (page, row, col)."""
    page_secs = cols * rows * secs_per_cell
    page = int(t // page_secs)
    remainder = t - page * page_secs
    cell = int(remainder // secs_per_cell)
    row = cell // cols
    col = cell % cols
    return page, row, col

def error_response(message: str, status: int = 400):
    return jsonify({"ok": False, "error": message}), status

def run_ffmpeg(cmd, timeout: int = 120):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if r.returncode == 0:
            return True, ""
        return False, (r.stderr or r.stdout or "ffmpeg failed")[-800:]
    except subprocess.TimeoutExpired:
        return False, "ffmpeg timed out"

def video_output_prefix(path: Path) -> str:
    return f"ep{episode_id_for_file(path)}" if re.match(r"^\d{1,3}(?:\D|$)", path.name) else safe_label(path.stem)

def sheet_metadata_path(sheet_path: Path) -> Path:
    return sheet_path.with_name(sheet_path.name + ".json")

def sheet_metadata(video_path: Path, duration: float, settings: dict, page: int) -> dict:
    return {
        "generator": "clip-picker-contact-sheet-v1",
        "source": str(video_path.resolve()),
        "source_mtime": video_path.stat().st_mtime,
        "duration": round(duration, 3),
        "page": page,
        "page_start": round(page * settings["page_secs"], 3),
        "page_secs": round(settings["page_secs"], 3),
        "seconds_per_frame": round(settings["secs"], 3),
        "cols": settings["cols"],
        "rows": settings["rows"],
    }

def sheet_metadata_matches(sheet_path: Path, expected: dict) -> bool:
    meta_path = sheet_metadata_path(sheet_path)
    if not sheet_path.exists() or not meta_path.exists():
        return False
    try:
        current = json.loads(meta_path.read_text(encoding="utf-8"))
    except Exception:
        return False
    return all(current.get(key) == value for key, value in expected.items())

def generate_contact_sheets(cfg: dict, force: bool = False) -> list[dict]:
    settings = runtime_settings(cfg)
    settings["sheet_dir"].mkdir(parents=True, exist_ok=True)
    results = []
    for episode in discover_episode_records(settings["episode_dir"], cfg):
        video_path = Path(episode["file"])
        duration = get_episode_duration(video_path)
        if duration <= 0:
            results.append({"id": episode["id"], "file": str(video_path), "ok": False, "error": "Could not read duration"})
            continue
        pages = int((duration + settings["page_secs"] - 0.001) // settings["page_secs"])
        prefix = video_output_prefix(video_path)
        generated = 0
        skipped = 0
        for page in range(pages):
            out = settings["sheet_dir"] / f"{prefix}_page{page}.jpg"
            meta = sheet_metadata(video_path, duration, settings, page)
            if not force and sheet_metadata_matches(out, meta):
                skipped += 1
                continue
            cmd = [
                "ffmpeg", "-y",
                "-ss", str(page * settings["page_secs"]),
                "-i", str(video_path),
                "-t", str(settings["page_secs"]),
                "-vf", (
                    f"fps=1/{settings['secs']},scale=360:-1,"
                    f"tile={settings['cols']}x{settings['rows']}:margin=2:padding=1:color=0x111111"
                ),
                "-frames:v", "1",
                str(out),
            ]
            ok, error = run_ffmpeg(cmd, timeout=180)
            if not ok:
                results.append({"id": episode["id"], "page": page, "ok": False, "error": error})
                continue
            sheet_metadata_path(out).write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            generated += 1
        results.append({
            "id": episode["id"],
            "file": str(video_path),
            "pages": pages,
            "generated": generated,
            "skipped": skipped,
            "ok": True,
        })
    return results

def set_job(job_id: str, **updates) -> None:
    with JOB_LOCK:
        EXPORT_JOBS.setdefault(job_id, {}).update(updates)

def find_prepare_script(cfg: dict) -> Path | None:
    configured = cfg.get("prepare_script")
    candidates = []
    if configured:
        candidates.append(cfg_path(cfg, "prepare_script", PROJECT_ROOT / "prepare.py"))
    candidates.extend([
        PROJECT_ROOT / "prepare.py",
        Path.home() / ".claude" / "skills" / "bilibili-video" / "prepare.py",
    ])
    for candidate in candidates:
        if candidate.exists() and candidate.is_file():
            return candidate
    return None

def fcpxml_path(cfg: dict) -> Path:
    configured = cfg.get("fcpxml_file")
    if configured:
        path = Path(configured).expanduser()
        return path if path.is_absolute() else PROJECT_ROOT / path
    return PROJECT_ROOT / "out" / "timeline.fcpxml"

def extract_clips(selections: dict, cfg: dict, settings: dict) -> list[dict]:
    results = []
    for seg in cfg["segments"]:
        sid = seg["id"]
        if sid not in selections:
            continue
        sel = selections[sid]
        ep = sel.get("episode", seg.get("suggested_episode", "01"))
        parts = sel.get("parts", [])
        if not parts:
            continue

        shot_dir = settings["output_dir"] / safe_label(sid)
        parts_dir = shot_dir / "parts"
        parts_dir.mkdir(parents=True, exist_ok=True)
        for old_part in parts_dir.glob("[0-9][0-9]_*.mp4"):
            old_part.unlink(missing_ok=True)

        ep_file = find_episode_file(settings["episode_dir"], cfg, ep)
        if not ep_file:
            results.append({"id": sid, "error": f"Episode {ep} not found", "ok": False})
            continue

        extracted_files = []
        for i, part in enumerate(parts):
            coerced = coerce_part(part)
            if not coerced:
                results.append({"id": sid, "part": i + 1, "error": "Invalid start/end", "ok": False})
                continue
            start_s, end_s = coerced
            dur_s = end_s - start_s
            out = parts_dir / f"{i+1:02d}_{safe_label(part.get('label', 'part'))}.mp4"
            cmd = [
                "ffmpeg", "-y",
                "-ss", str(start_s), "-i", str(ep_file),
                "-t", str(dur_s), "-c", "copy",
                str(out),
            ]
            ok, error = run_ffmpeg(cmd)
            if ok:
                extracted_files.append(out)
                results.append({"id": sid, "part": i + 1, "file": str(out), "size": out.stat().st_size, "ok": True})
            else:
                results.append({"id": sid, "part": i + 1, "error": error, "ok": False})

        if not extracted_files:
            continue
        concat_list = parts_dir / "_concat.txt"
        with open(concat_list, "w", encoding="utf-8") as fh:
            for pf in extracted_files:
                fh.write(concat_file_line(pf))
        clip_out = shot_dir / "clip.mp4"
        ok, error = run_ffmpeg(
            ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(concat_list),
             "-c", "copy", str(clip_out)])
        concat_list.unlink(missing_ok=True)
        if ok:
            results.append({"id": sid, "merged": str(clip_out), "ok": True})
        else:
            results.append({"id": sid, "merged": str(clip_out), "error": error, "ok": False})
    return results

def export_worker(job_id: str, export_type: str, selections: dict) -> None:
    try:
        cfg = load_cfg()
        settings = runtime_settings(cfg)
        set_job(job_id, status="running", step="保存 selections.json", progress=10)
        save_selections(selections)

        if export_type == "selections":
            set_job(job_id, status="done", step="完成", progress=100, file=str(SEL_FILE), download="/api/download/selections")
            return

        if export_type != "fcpxml":
            raise ValueError("Unsupported export type")

        set_job(job_id, step="提取 clips", progress=30)
        results = extract_clips(selections, cfg, settings)
        failed = [item for item in results if item.get("ok") is False]
        if failed:
            first = failed[0]
            raise RuntimeError(first.get("error") or "Clip extraction failed")

        prepare_script = find_prepare_script(cfg)
        if not prepare_script:
            raise FileNotFoundError("No prepare.py found. Set prepare_script in segments.json or install the bilibili-video skill.")

        set_job(job_id, step="生成 FCPXML", progress=70)
        r = subprocess.run(
            [sys.executable, str(prepare_script)],
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
            timeout=900,
        )
        if r.returncode != 0:
            raise RuntimeError((r.stderr or r.stdout or "prepare.py failed")[-1200:])

        out = fcpxml_path(cfg)
        if not out.exists():
            raise FileNotFoundError(f"prepare.py finished but FCPXML was not found at {out}")
        set_job(job_id, status="done", step="完成", progress=100, file=str(out), download="/api/download/fcpxml")
    except Exception as exc:
        set_job(job_id, status="error", step="失败", progress=100, error=str(exc))

# ── Flask app ───────────────────────────────────────────────────────
from flask import Flask, jsonify, request, send_file

app = Flask(__name__, static_folder=str(APP_ROOT))

@app.route("/")
def index():
    return send_file(str(APP_ROOT / "clip_picker.html"))

@app.route("/api/config")
def api_config():
    try:
        cfg = load_cfg()
        settings = runtime_settings(cfg)
    except Exception as exc:
        return error_response(str(exc), 500)

    episodes = discover_episode_records(settings["episode_dir"], cfg)
    # Build episode info
    ep_info = {}
    for episode in episodes:
        sheets = discover_sheets(settings["sheet_dir"], episode)
        dur = get_episode_duration(Path(episode["file"]))
        timing = episode_timing(sheets, dur, settings)
        ep_info[episode["id"]] = {
            "title": episode["title"],
            "file": episode["file"],
            "duration": round(dur, 1),
            "pages": timing["pages"],
            "page_secs": timing["page_secs"],
            "secs_per_cell": timing["secs_per_cell"],
            "valid_cells": max(0, min(timing["pages"] * settings["cols"] * settings["rows"], int((dur + timing["secs_per_cell"] - 0.001) // timing["secs_per_cell"]))) if dur > 0 else timing["pages"] * settings["cols"] * settings["rows"],
            "sheets": {str(k): v for k, v in sheets.items()},
            "subtitles": episode_subtitles(cfg, settings, episode),
        }
    # Load existing selections
    sels = load_selections()
    return jsonify({
        "title": cfg["title"],
        "segments": cfg["segments"],
        "episodes": ep_info,
        "episode_list": [episode["id"] for episode in episodes],
        "sheet_cols": settings["cols"],
        "sheet_rows": settings["rows"],
        "secs_per_cell": settings["secs"],
        "page_secs": settings["page_secs"],
        "selections": sels,
    })

@app.route("/contact_sheets/<path:filename>")
def serve_sheet(filename):
    try:
        settings = runtime_settings(load_cfg())
    except Exception as exc:
        return error_response(str(exc), 500)
    safe_name = Path(filename).name
    p = settings["sheet_dir"] / safe_name
    if p.exists() and p.is_file():
        return send_file(str(p))
    return ("not found", 404)

@app.route("/api/save", methods=["POST"])
def api_save():
    data = request.get_json()
    try:
        save_selections(data)
        return jsonify({"ok": True, "path": str(SEL_FILE)})
    except Exception as exc:
        return error_response(str(exc), 400)

@app.route("/api/extract", methods=["POST"])
def api_extract():
    """Extract clips based on selections.  Requires segments.json + selections."""
    data = request.get_json(silent=True) or {}
    selections = data.get("selections", {})
    if not isinstance(selections, dict):
        return error_response("selections must be an object")
    try:
        cfg = load_cfg()
        settings = runtime_settings(cfg)
    except Exception as exc:
        return error_response(str(exc), 500)
    return jsonify({"results": extract_clips(selections, cfg, settings)})

@app.route("/api/export", methods=["POST"])
def api_export():
    data = request.get_json(silent=True) or {}
    selections = data.get("selections", {})
    export_type = data.get("type", "selections")
    if not isinstance(selections, dict):
        return error_response("selections must be an object")
    job_id = uuid.uuid4().hex
    with JOB_LOCK:
        EXPORT_JOBS[job_id] = {
            "id": job_id,
            "status": "queued",
            "step": "排队中",
            "progress": 0,
            "type": export_type,
            "created_at": time.time(),
        }
    thread = threading.Thread(target=export_worker, args=(job_id, export_type, selections), daemon=True)
    thread.start()
    return jsonify({"ok": True, "job_id": job_id})

@app.route("/api/export/<job_id>")
def api_export_status(job_id):
    with JOB_LOCK:
        job = EXPORT_JOBS.get(job_id)
        if not job:
            return error_response("Export job not found", 404)
        return jsonify(job)

@app.route("/api/download/selections")
def download_selections():
    if not SEL_FILE.exists():
        return error_response("selections.json has not been exported yet", 404)
    return send_file(str(SEL_FILE), as_attachment=True, download_name="selections.json")

@app.route("/api/download/fcpxml")
def download_fcpxml():
    try:
        path = fcpxml_path(load_cfg())
    except Exception as exc:
        return error_response(str(exc), 500)
    if not path.exists():
        return error_response("FCPXML has not been generated yet", 404)
    return send_file(str(path), as_attachment=True, download_name=path.name)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Visual contact-sheet clip picker")
    parser.add_argument("command", nargs="?", choices=["serve", "generate-sheets"], default="serve", help="Run the web UI or generate contact sheets")
    parser.add_argument("--config", default=None, help="Path to segments.json")
    parser.add_argument("--selections", default=None, help="Path to selections.json")
    parser.add_argument("--host", default=HOST, help="Bind host")
    parser.add_argument("--port", type=int, default=PORT, help="Bind port")
    parser.add_argument("--force", action="store_true", help="Regenerate existing contact sheets")
    args = parser.parse_args()
    configure_paths(args.config, args.selections)
    if args.command == "generate-sheets":
        for item in generate_contact_sheets(load_cfg(), force=args.force):
            print(json.dumps(item, ensure_ascii=False))
        raise SystemExit(0)
    print(f"Clip Picker → http://{args.host}:{args.port}")
    print(f"Config → {CFG_FILE}")
    app.run(host=args.host, port=args.port, debug=False)
