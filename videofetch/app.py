"""VideoFetch clone: single + bulk video downloader / metadata scraper.

Backend is FastAPI + yt-dlp (supports YouTube, TikTok, Instagram, X/Twitter,
Facebook, Vimeo, Reddit, Dailymotion, SoundCloud and ~1800 other sites).

Run:  uvicorn app:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import os
import re
import shutil
import tempfile
import threading
import time
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import yt_dlp
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask

BASE_DIR = Path(__file__).parent
WORK_DIR = Path(os.environ.get("VF_WORK_DIR", tempfile.gettempdir())) / "videofetch"
WORK_DIR.mkdir(parents=True, exist_ok=True)

MAX_BULK_URLS = int(os.environ.get("VF_MAX_BULK_URLS", "200"))
MAX_SCRAPE_ITEMS = int(os.environ.get("VF_MAX_SCRAPE_ITEMS", "500"))
WORKERS = int(os.environ.get("VF_WORKERS", "6"))
JOB_TTL_SECONDS = int(os.environ.get("VF_JOB_TTL", "3600"))
COOKIES_FILE = os.environ.get("VF_COOKIES_FILE")  # optional Netscape cookies.txt

# YouTube needs a JS runtime (deno or node) plus the yt-dlp-ejs solver package.
JS_RUNTIMES = {name: {} for name in ("deno", "node") if shutil.which(name)}

pool = ThreadPoolExecutor(max_workers=WORKERS)
app = FastAPI(title="VideoFetch", version="1.0.0")
# Allow the standalone videofetch.html to call this API from another origin.
app.add_middleware(CORSMiddleware, allow_origins=os.environ.get("VF_CORS_ORIGINS", "*").split(","),
                   allow_methods=["*"], allow_headers=["*"], expose_headers=["Content-Disposition"])


# --------------------------------------------------------------------------- #
# yt-dlp helpers
# --------------------------------------------------------------------------- #

def _base_opts() -> dict:
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "socket_timeout": 30,
        "retries": 3,
    }
    if COOKIES_FILE:
        opts["cookiefile"] = COOKIES_FILE
    if JS_RUNTIMES:
        opts["js_runtimes"] = JS_RUNTIMES
    return opts


def _clean_url(url: str) -> str:
    url = url.strip()
    if url and not re.match(r"^https?://", url, re.I):
        url = "https://" + url
    return url


def _human_size(n: float | None) -> str | None:
    if not n:
        return None
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def _summarize_formats(info: dict) -> list[dict]:
    """Collapse yt-dlp's format list into user-friendly choices."""
    duration = info.get("duration") or 0
    choices: dict[str, dict] = {}

    for f in info.get("formats") or []:
        vcodec, acodec = f.get("vcodec"), f.get("acodec")
        height = f.get("height")
        size = f.get("filesize") or f.get("filesize_approx")
        if not size and f.get("tbr") and duration:
            size = f["tbr"] * 1000 / 8 * duration
        if vcodec and vcodec != "none" and height:
            key = f"{height}p"
            prev = choices.get(key)
            if not prev or (size or 0) > (prev["_size"] or 0):
                choices[key] = {
                    "id": key,
                    "kind": "video",
                    "label": f"{height}p" + (f" {int(f['fps'])}fps" if (f.get("fps") or 0) > 30 else ""),
                    "ext": "mp4",
                    "height": height,
                    "_size": size,
                }

    out = sorted(choices.values(), key=lambda c: c["height"], reverse=True)
    if not out:
        # Sites that expose a single progressive file without height info.
        out.append({"id": "best", "kind": "video", "label": "Best", "ext": info.get("ext") or "mp4", "height": None, "_size": info.get("filesize")})
    out.append({"id": "mp3", "kind": "audio", "label": "MP3 (audio)", "ext": "mp3", "height": None, "_size": None})
    out.append({"id": "m4a", "kind": "audio", "label": "M4A (audio)", "ext": "m4a", "height": None, "_size": None})
    for c in out:
        c["size"] = _human_size(c.pop("_size"))
    return out


def _format_selector(choice: str) -> tuple[str, list[dict], str | None]:
    """Map a user choice ("720p", "best", "mp3", "m4a") to yt-dlp options."""
    if choice in ("mp3", "m4a"):
        return (
            "bestaudio/best",
            [{"key": "FFmpegExtractAudio", "preferredcodec": choice, "preferredquality": "192"}],
            None,
        )
    m = re.fullmatch(r"(\d+)p", choice or "")
    if m:
        h = m.group(1)
        sel = (
            f"bestvideo[height<={h}][ext=mp4]+bestaudio[ext=m4a]/"
            f"bestvideo[height<={h}]+bestaudio/best[height<={h}]/best"
        )
    else:
        sel = "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best"
    return sel, [], "mp4"


def extract_info(url: str) -> dict:
    url = _clean_url(url)
    with yt_dlp.YoutubeDL(_base_opts()) as ydl:
        info = ydl.extract_info(url, download=False)
    if info.get("_type") == "playlist" and info.get("entries"):
        info = next(e for e in info["entries"] if e)
    thumb = info.get("thumbnail")
    if not thumb and info.get("thumbnails"):
        thumb = info["thumbnails"][-1].get("url")
    return {
        "url": info.get("webpage_url") or url,
        "id": info.get("id"),
        "title": info.get("title") or "Untitled",
        "uploader": info.get("uploader") or info.get("channel") or info.get("creator"),
        "platform": info.get("extractor_key") or info.get("extractor"),
        "duration": info.get("duration"),
        "duration_string": info.get("duration_string"),
        "view_count": info.get("view_count"),
        "like_count": info.get("like_count"),
        "comment_count": info.get("comment_count"),
        "upload_date": info.get("upload_date"),
        "description": (info.get("description") or "")[:500],
        "thumbnail": thumb,
        "formats": _summarize_formats(info),
    }


def scrape_collection(url: str, limit: int) -> dict:
    """Flat-extract a playlist / channel / profile / hashtag page into video URLs."""
    url = _clean_url(url)
    opts = _base_opts() | {"noplaylist": False, "extract_flat": "in_playlist", "playlistend": limit}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)

    items: list[dict] = []

    def walk(node: dict):
        for e in node.get("entries") or []:
            if not e or len(items) >= limit:
                continue
            # YouTube channels nest tabs (Videos / Shorts / Live) as sub-playlists.
            if e.get("_type") == "playlist" and e.get("entries"):
                walk(e)
                continue
            link = e.get("url") or e.get("webpage_url")
            if link and not link.startswith("http") and e.get("ie_key") == "Youtube":
                link = f"https://www.youtube.com/watch?v={link}"
            if not link:
                continue
            thumbs = e.get("thumbnails") or []
            items.append({
                "url": link,
                "id": e.get("id"),
                "title": e.get("title") or link,
                "duration": e.get("duration"),
                "view_count": e.get("view_count"),
                "thumbnail": e.get("thumbnail") or (thumbs[-1]["url"] if thumbs else None),
                "uploader": e.get("uploader") or e.get("channel") or info.get("uploader"),
            })

    if info.get("_type") in ("playlist", "multi_video") or info.get("entries"):
        walk(info)
    else:
        items.append({"url": info.get("webpage_url") or url, "id": info.get("id"), "title": info.get("title"),
                      "duration": info.get("duration"), "view_count": info.get("view_count"),
                      "thumbnail": info.get("thumbnail"), "uploader": info.get("uploader")})
    return {"title": info.get("title") or url, "uploader": info.get("uploader"), "count": len(items), "items": items}


def download_one(url: str, choice: str, dest: Path, progress_cb=None) -> Path:
    dest.mkdir(parents=True, exist_ok=True)
    sel, post, merge = _format_selector(choice)
    opts = _base_opts() | {
        "format": sel,
        "outtmpl": str(dest / "%(title).150B [%(id)s].%(ext)s"),
        "postprocessors": post,
        "restrictfilenames": False,
        "windowsfilenames": True,
    }
    if merge:
        opts["merge_output_format"] = merge
    if progress_cb:
        opts["progress_hooks"] = [progress_cb]
    before = set(dest.iterdir())
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([_clean_url(url)])
    new = [p for p in dest.iterdir() if p not in before and not p.name.endswith((".part", ".ytdl"))]
    if not new:
        raise RuntimeError("Download produced no file")
    return max(new, key=lambda p: p.stat().st_size)


# --------------------------------------------------------------------------- #
# Bulk download jobs (in-memory)
# --------------------------------------------------------------------------- #

class Job:
    def __init__(self, items: list[dict], zip_name: str):
        self.id = uuid.uuid4().hex
        self.dir = WORK_DIR / self.id
        self.items = [{"url": i["url"], "format": i.get("format", "best"), "title": i.get("title"),
                       "status": "queued", "progress": 0.0, "error": None, "file": None} for i in items]
        self.zip_name = zip_name
        self.zip_path: Path | None = None
        self.status = "running"
        self.created = time.time()
        self.lock = threading.Lock()

    def public(self) -> dict:
        with self.lock:
            done = sum(1 for i in self.items if i["status"] in ("done", "error"))
            return {
                "id": self.id,
                "status": self.status,
                "total": len(self.items),
                "completed": done,
                "succeeded": sum(1 for i in self.items if i["status"] == "done"),
                "items": [{k: v for k, v in i.items() if k != "file"} for i in self.items],
                "zip_ready": self.zip_path is not None,
            }


JOBS: dict[str, Job] = {}


def _run_item(job: Job, idx: int):
    item = job.items[idx]
    item["status"] = "downloading"

    def hook(d):
        if d.get("status") == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            if total:
                item["progress"] = round(d.get("downloaded_bytes", 0) / total * 100, 1)
        elif d.get("status") == "finished":
            item["progress"] = 100.0
            item["status"] = "processing"

    try:
        path = download_one(item["url"], item["format"], job.dir / f"{idx:04d}", hook)
        item["file"] = str(path)
        item["title"] = item["title"] or path.stem
        item["status"] = "done"
        item["progress"] = 100.0
    except Exception as e:  # noqa: BLE001 - report any extractor failure to the user
        item["status"] = "error"
        item["error"] = _short_error(e)


def _run_job(job: Job):
    futures = [pool.submit(_run_item, job, i) for i in range(len(job.items))]
    for f in futures:
        f.result()
    files = [Path(i["file"]) for i in job.items if i["file"]]
    if files:
        zip_path = job.dir / job.zip_name
        used: set[str] = set()
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_STORED) as zf:
            for p in files:
                name = p.name
                n = 1
                while name in used:
                    name = f"{p.stem} ({n}){p.suffix}"
                    n += 1
                used.add(name)
                zf.write(p, name)
        job.zip_path = zip_path
    job.status = "finished"


def _cleanup_loop():
    while True:
        time.sleep(300)
        now = time.time()
        for jid, job in list(JOBS.items()):
            if now - job.created > JOB_TTL_SECONDS and job.status == "finished":
                shutil.rmtree(job.dir, ignore_errors=True)
                JOBS.pop(jid, None)


threading.Thread(target=_cleanup_loop, daemon=True).start()


def _short_error(e: Exception) -> str:
    msg = re.sub(r"\x1b\[[0-9;]*m", "", str(e))
    msg = msg.replace("ERROR: ", "")
    return msg[:300]


# --------------------------------------------------------------------------- #
# API
# --------------------------------------------------------------------------- #

class InfoReq(BaseModel):
    url: str


class BulkInfoReq(BaseModel):
    urls: list[str] = Field(..., min_length=1)


class ScrapeReq(BaseModel):
    url: str
    limit: int = 50


class JobItem(BaseModel):
    url: str
    format: str = "best"
    title: str | None = None


class JobReq(BaseModel):
    items: list[JobItem] = Field(..., min_length=1)
    zip_name: str = "videofetch.zip"


@app.post("/api/info")
def api_info(req: InfoReq):
    try:
        return extract_info(req.url)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, _short_error(e))


@app.post("/api/bulk-info")
def api_bulk_info(req: BulkInfoReq):
    urls = list(dict.fromkeys(u.strip() for u in req.urls if u.strip()))[:MAX_BULK_URLS]

    def safe(u):
        try:
            return {"ok": True, **extract_info(u)}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "url": u, "error": _short_error(e)}

    return {"results": list(pool.map(safe, urls))}


@app.post("/api/scrape")
def api_scrape(req: ScrapeReq):
    try:
        return scrape_collection(req.url, max(1, min(req.limit, MAX_SCRAPE_ITEMS)))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, _short_error(e))


@app.get("/api/download")
def api_download(url: str, format: str = "best"):
    tmp = WORK_DIR / ("single-" + uuid.uuid4().hex)
    try:
        path = download_one(url, format, tmp)
    except Exception as e:  # noqa: BLE001
        shutil.rmtree(tmp, ignore_errors=True)
        raise HTTPException(400, _short_error(e))
    return FileResponse(path, filename=path.name, media_type="application/octet-stream",
                        background=BackgroundTask(shutil.rmtree, tmp, ignore_errors=True))


@app.post("/api/jobs")
def api_create_job(req: JobReq):
    if len(req.items) > MAX_BULK_URLS:
        raise HTTPException(400, f"Max {MAX_BULK_URLS} items per job")
    name = re.sub(r"[^\w.\- ]", "_", req.zip_name) or "videofetch.zip"
    if not name.endswith(".zip"):
        name += ".zip"
    job = Job([i.model_dump() for i in req.items], name)
    JOBS[job.id] = job
    threading.Thread(target=_run_job, args=(job,), daemon=True).start()
    return job.public()


@app.get("/api/jobs/{job_id}")
def api_job(job_id: str):
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    return job.public()


@app.get("/api/jobs/{job_id}/zip")
def api_job_zip(job_id: str):
    job = JOBS.get(job_id)
    if not job or not job.zip_path:
        raise HTTPException(404, "ZIP not ready")
    return FileResponse(job.zip_path, filename=job.zip_name, media_type="application/zip")


@app.get("/api/jobs/{job_id}/file/{idx}")
def api_job_file(job_id: str, idx: int):
    job = JOBS.get(job_id)
    if not job or not (0 <= idx < len(job.items)) or not job.items[idx]["file"]:
        raise HTTPException(404, "File not ready")
    p = Path(job.items[idx]["file"])
    return FileResponse(p, filename=p.name, media_type="application/octet-stream")


@app.get("/api/health")
def health():
    return JSONResponse({"ok": True, "yt_dlp": yt_dlp.version.__version__, "ffmpeg": bool(shutil.which("ffmpeg")),
                         "js_runtimes": list(JS_RUNTIMES)})


app.mount("/", StaticFiles(directory=BASE_DIR / "static", html=True), name="static")
