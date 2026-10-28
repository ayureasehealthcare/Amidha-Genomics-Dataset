# VideoFetch — single & bulk video downloader / scraper

A self-hosted clone of videofetch.app-style downloaders, with **bulk** support.

## Features
- **Single link**: paste a URL to see a preview (thumbnail, uploader, duration, views, likes, upload date) and download as MP4 (144p–4K) or MP3/M4A.
- **Bulk links**: paste up to 200 URLs, or import a `.txt`/`.csv`. Metadata for all of them is fetched in parallel.
- **Playlist / channel scrape**: pull up to 500 videos from a YouTube playlist or channel, TikTok profile, SoundCloud set, Vimeo showcase, and similar pages.
- **Bulk download**: select rows, pick a quality, and download everything as one **ZIP**, with progress for each video. Single files can also be saved one by one.
- **Export** scraped metadata to **CSV / JSON**, or copy all links.
- Works with YouTube, TikTok, Instagram, X, Facebook, Reddit, Vimeo, Dailymotion and the other ~1800 sites [yt-dlp](https://github.com/yt-dlp/yt-dlp) supports.
- Light and dark themes, works on phones, no sign-up. Temp files are deleted automatically after 1 hour.

## Run locally
Requires Python 3.10+, **ffmpeg**, and **Deno or Node.js** (YouTube needs a JS runtime).

```bash
cd videofetch
pip install -r requirements.txt
uvicorn app:app --host 0.0.0.0 --port 8000
# open http://localhost:8000
```

## Standalone HTML
`videofetch.html` is the whole frontend in one file, with the CSS and JS inside it. You can host it anywhere (Netlify, GitHub Pages, cPanel) or open it straight from disk. Point it at your backend in either of two ways:
- add `?api=https://your-backend.com` to the URL, or
- edit `window.VF_API = "https://your-backend.com"` near the bottom of the file.

The backend sends CORS headers. Use `VF_CORS_ORIGINS=https://yoursite.com` to restrict which sites can call it.

## Docker
```bash
docker build -t videofetch ./videofetch
docker run -p 8000:8000 videofetch
```

## Configuration (env vars)
| Var | Default | Meaning |
|---|---|---|
| `VF_WORKERS` | 6 | parallel fetch/download threads |
| `VF_MAX_BULK_URLS` | 200 | max links per bulk request/job |
| `VF_MAX_SCRAPE_ITEMS` | 500 | max videos per playlist/channel scrape |
| `VF_JOB_TTL` | 3600 | seconds before finished job files are deleted |
| `VF_WORK_DIR` | system temp | where downloads are staged |
| `VF_CORS_ORIGINS` | * | comma-separated origins allowed to call the API |
| `VF_COOKIES_FILE` | – | Netscape `cookies.txt` for login-only, private or age-restricted content (e.g. Instagram) |

## API
| Method | Path | Body / params |
|---|---|---|
| POST | `/api/info` | `{url}` → metadata + formats |
| POST | `/api/bulk-info` | `{urls: [...]}` |
| POST | `/api/scrape` | `{url, limit}` → playlist/channel entries |
| GET | `/api/download` | `?url=&format=best|1080p|720p|…|mp3|m4a` → file |
| POST | `/api/jobs` | `{items:[{url, format}], zip_name}` → job |
| GET | `/api/jobs/{id}` | progress |
| GET | `/api/jobs/{id}/zip` | ZIP of finished files |
| GET | `/api/jobs/{id}/file/{idx}` | single finished file |

## Notes
- Host it on a residential or VPS IP. YouTube often blocks video downloads from big cloud/datacenter IPs (HTTP 403), even when metadata works. If that happens, set `VF_COOKIES_FILE` or use a proxy.
- Only download content you own or have permission to download, and follow each platform's terms of service.
