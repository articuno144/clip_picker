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
    sheets = {}
    ep_id = episode["id"]
    stem = safe_label(Path(episode["file"]).stem)
    prefixes = [f"ep{ep_id}_page", f"{ep_id}_page", f"{stem}_page"]
    if sheet_dir.exists():
        for f in sheet_dir.iterdir():
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
    return sheets

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
    return max(1, from_duration, from_sheets)

def episode_timing(sheets: dict, duration: float, settings: dict) -> dict:
    pages = page_count_from_sheets_and_duration(sheets, duration, settings["page_secs"])
    if sheets and duration > 0:
        page_secs = duration / pages
    else:
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
            "sheets": {str(k): v for k, v in sheets.items()},
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

        # Find episode file
        ep_file = find_episode_file(settings["episode_dir"], cfg, ep)
        if not ep_file:
            results.append({"id": sid, "error": f"Episode {ep} not found"})
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
                sz = out.stat().st_size
                results.append({"id": sid, "part": i+1, "file": str(out), "size": sz, "ok": True})
            else:
                results.append({"id": sid, "part": i+1, "error": error, "ok": False})

        # Merge parts into clip.mp4
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

    return jsonify({"results": results})


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Visual contact-sheet clip picker")
    parser.add_argument("--config", default=None, help="Path to segments.json")
    parser.add_argument("--selections", default=None, help="Path to selections.json")
    parser.add_argument("--host", default=HOST, help="Bind host")
    parser.add_argument("--port", type=int, default=PORT, help="Bind port")
    args = parser.parse_args()
    configure_paths(args.config, args.selections)
    print(f"Clip Picker → http://{args.host}:{args.port}")
    print(f"Config → {CFG_FILE}")
    app.run(host=args.host, port=args.port, debug=False)
