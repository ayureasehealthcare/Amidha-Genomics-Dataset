#!/usr/bin/env python3
"""Polite scraper that builds a directory of radio stations listed on tamilradios.com.

Station pages come from the site's sitemap.xml (or a same-host crawl if no sitemap
is available). Each page carries two data sources that are merged into one record:
  * the Next.js page payload's `"station": {...}` object (stream URL, bitrate, genre, images)
  * the schema.org RadioStation JSON-LD block (description, frequency, location, links)

Only metadata is collected; broadcasts are never recorded or downloaded.

Usage:
    python scrape.py --out stations.jsonl
    python scrape.py --out stations.csv --check-streams
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from collections import deque
from urllib.parse import urldefrag, urljoin, urlparse
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup

START_URL = "https://www.tamilradios.com/"
USER_AGENT = "TamilRadioDirectoryBot/1.0 (+metadata-only)"
SKIP_EXT = re.compile(r"\.(?:jpe?g|png|gif|webp|svg|ico|css|js|pdf|zip|woff2?|ttf|xml)(?:\?|$)", re.I)
RSC_CHUNK = re.compile(r'self\.__next_f\.push\(\[1,"((?:[^"\\]|\\.)*)"\]\)')
STATION_FIELDS = ("id", "slug", "name", "mobile_title", "url", "web_stream_url", "bitrate",
                  "genre", "country", "image", "image_wide", "is_active")
CSV_COLUMNS = ("name", "slug", "page_url", "stream_url", "web_stream_url", "bitrate", "genre",
               "frequency", "city", "region", "country", "language", "founded", "description",
               "image", "website_links", "stream_status")

log = logging.getLogger("tamilradios")


class Client:
    def __init__(self, start_url: str, delay: float, user_agent: str, timeout: float = 60):
        self.host = urlparse(start_url).netloc
        self.delay = delay
        self.timeout = timeout
        self.user_agent = user_agent
        self.session = requests.Session()
        self.session.headers["User-Agent"] = user_agent
        # Fetch robots.txt through the same session (proxy/CA settings) rather than urllib,
        # whose failures make RobotFileParser silently disallow everything.
        self.robots = RobotFileParser()
        try:
            r = self.session.get(urljoin(start_url, "/robots.txt"), timeout=20)
            self.robots.parse(r.text.splitlines() if r.status_code == 200 else [])
        except requests.RequestException as e:
            log.warning("could not read robots.txt (%s); continuing", e)
            self.robots.parse([])
        self._last = 0.0

    def allowed(self, url: str) -> bool:
        try:
            return self.robots.can_fetch(self.user_agent, url)
        except Exception:
            return True

    def get(self, url: str, want: str = "html") -> str | None:
        for attempt in range(3):
            wait = self.delay - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()
            try:
                r = self.session.get(url, timeout=self.timeout)
            except requests.RequestException as e:
                log.warning("GET %s failed (%s), attempt %d", url, e, attempt + 1)
                time.sleep(2 ** (attempt + 1))
                continue
            if r.status_code in (429, 503):
                time.sleep(int(r.headers.get("Retry-After", 2 ** (attempt + 2))))
                continue
            if r.status_code != 200 or want not in r.headers.get("Content-Type", ""):
                return None
            return r.text
        return None

    def same_host(self, base: str, href: str) -> str | None:
        url, _ = urldefrag(urljoin(base, href.strip()))
        p = urlparse(url)
        if p.scheme not in ("http", "https") or p.netloc != self.host or SKIP_EXT.search(p.path):
            return None
        return url

    def check_stream(self, url: str) -> str:
        """Read only the response headers to see whether a stream is live."""
        try:
            with self.session.get(url, stream=True, timeout=10) as r:
                return f"{r.status_code} {r.headers.get('Content-Type', '?')}"
        except requests.RequestException as e:
            return f"error: {type(e).__name__}"


def sitemap_urls(client: Client, start_url: str) -> list[str]:
    xml = client.get(urljoin(start_url, "/sitemap.xml"), want="xml")
    if not xml:
        return []
    ns = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    root = ET.fromstring(xml.encode())
    urls = [loc.text.strip() for loc in root.iterfind(".//s:loc", ns) if loc.text]
    if root.tag.endswith("sitemapindex"):  # nested sitemaps
        nested = []
        for sm in urls:
            sub = client.get(sm, want="xml")
            if sub:
                nested += [l.text.strip() for l in ET.fromstring(sub.encode()).iterfind(".//s:loc", ns) if l.text]
        urls = nested
    return urls


def crawl_urls(client: Client, start_url: str, max_pages: int) -> list[str]:
    queue, seen, out = deque([start_url]), {start_url}, []
    while queue and len(out) < max_pages:
        url = queue.popleft()
        if not client.allowed(url) or not (html := client.get(url)):
            continue
        out.append(url)
        for a in BeautifulSoup(html, "html.parser").find_all("a", href=True):
            link = client.same_host(url, a["href"])
            if link and link not in seen:
                seen.add(link)
                queue.append(link)
    return out


def page_payload(html: str) -> str:
    parts = []
    for chunk in RSC_CHUNK.findall(html):
        try:
            parts.append(json.loads(f'"{chunk}"'))
        except json.JSONDecodeError:
            pass
    return "".join(parts)


def station_object(payload: str, slug: str) -> dict | None:
    dec = json.JSONDecoder()
    for m in re.finditer(r'"station":\{', payload):
        try:
            obj, _ = dec.raw_decode(payload, m.end() - 1)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and obj.get("slug") == slug:
            return obj
    return None


def json_ld_station(soup: BeautifulSoup) -> dict:
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or "")
        except json.JSONDecodeError:
            continue
        for item in data if isinstance(data, list) else [data]:
            if isinstance(item, dict) and item.get("@type") == "RadioStation":
                return item
    return {}


def extract(url: str, html: str) -> dict | None:
    slug = urlparse(url).path.strip("/")
    if not slug or "/" in slug:
        return None
    soup = BeautifulSoup(html, "html.parser")
    obj = station_object(page_payload(html), slug) or {}
    ld = json_ld_station(soup)
    if not obj and not ld:
        return None
    addr = ld.get("address") or {}
    genre = obj.get("genre") or ld.get("genre")
    return {
        "name": obj.get("name") or ld.get("name"),
        "slug": slug,
        "page_url": url,
        "stream_url": obj.get("url"),
        "web_stream_url": obj.get("web_stream_url"),
        "bitrate": obj.get("bitrate"),
        "genre": ", ".join(genre) if isinstance(genre, list) else genre,
        "frequency": ld.get("broadcastFrequency"),
        "city": addr.get("addressLocality"),
        "region": addr.get("addressRegion"),
        "country": addr.get("addressCountry") or obj.get("country") or ld.get("areaServed"),
        "language": ld.get("inLanguage"),
        "founded": ld.get("foundingDate"),
        "description": ld.get("description"),
        "image": urljoin(url, img) if (img := obj.get("image") or ld.get("image")) else None,
        "website_links": ld.get("sameAs") or [],
        "station_id": obj.get("id"),
        "is_active": obj.get("is_active"),
    }


def write(rows: list[dict], path: str) -> None:
    if path.endswith(".csv"):
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=CSV_COLUMNS, extrasaction="ignore")
            w.writeheader()
            for r in rows:
                w.writerow({**r, "website_links": " | ".join(r["website_links"]),
                            "stream_status": r.get("stream_status", "")})
    else:
        with open(path, "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--start-url", default=START_URL)
    ap.add_argument("--out", default="stations.jsonl", help=".jsonl or .csv")
    ap.add_argument("--max-pages", type=int, default=1000)
    ap.add_argument("--delay", type=float, default=1.5, help="seconds between requests (min 1.0)")
    ap.add_argument("--check-streams", action="store_true", help="probe each stream URL's headers")
    ap.add_argument("--timeout", type=float, default=60, help="per-request timeout in seconds")
    ap.add_argument("--contact", help="email/URL to include in the User-Agent")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(levelname)s %(message)s")
    ua = f"{USER_AGENT[:-1]}; {args.contact})" if args.contact else USER_AGENT
    client = Client(args.start_url, max(args.delay, 1.0), ua, args.timeout)

    urls = sitemap_urls(client, args.start_url)
    if urls:
        log.info("sitemap lists %d URLs", len(urls))
        urls = [u for u in urls if client.same_host(args.start_url, u) and client.allowed(u)][: args.max_pages]
    else:
        log.info("no sitemap; crawling")
        urls = crawl_urls(client, args.start_url, args.max_pages)

    # Each station is appended to a progress file as soon as it is scraped, so a slow or
    # interrupted run keeps its results and a rerun skips pages already done.
    progress = f"{args.out}.progress.jsonl"
    rows = []
    try:
        with open(progress, encoding="utf-8") as f:
            rows = [json.loads(line) for line in f if line.strip()]
        log.info("resuming: %d stations already in %s", len(rows), progress)
    except FileNotFoundError:
        pass
    done = {r["page_url"] for r in rows}
    with open(progress, "a", encoding="utf-8") as pf:
        for i, url in enumerate(urls, 1):
            if url in done or not urlparse(url).path.strip("/") or not (html := client.get(url)):
                continue
            if row := extract(url, html):
                rows.append(row)
                pf.write(json.dumps(row, ensure_ascii=False) + "\n")
                pf.flush()
                log.info("[%d/%d] %s -> %s", i, len(urls), row["name"], row["stream_url"])

    if args.check_streams:
        for r in rows:
            if r["stream_url"]:
                r["stream_status"] = client.check_stream(r["stream_url"])
                time.sleep(0.5)

    rows.sort(key=lambda r: (r["name"] or "").lower())
    write(rows, args.out)
    os.remove(progress)
    log.info("wrote %d stations to %s", len(rows), args.out)
    return 0 if rows else 1


if __name__ == "__main__":
    sys.exit(main())
