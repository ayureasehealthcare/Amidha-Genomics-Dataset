#!/usr/bin/env python3
"""Polite crawler that builds a directory of radio stations listed on tamilradios.com.

For each station page it records: name, page URL, stream URL(s), logo, description
and the category/listing pages it was found on. It collects metadata only; it does
not record or download broadcasts.

Usage:
    python scrape.py --out stations.jsonl
    python scrape.py --out stations.csv --max-pages 300 --delay 2 --check-streams
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import re
import sys
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from urllib.parse import urldefrag, urljoin, urlparse
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup

START_URL = "https://www.tamilradios.com/"
USER_AGENT = "TamilRadioDirectoryBot/1.0 (+metadata-only; contact: set --contact)"

# Stream-looking URLs embedded anywhere in HTML or inline JS.
STREAM_RE = re.compile(
    r"""https?://[^\s"'<>\\]+?(?:"""
    r"""\.(?:mp3|aac|aacp|ogg|opus|m3u8?|pls)(?:\?[^\s"'<>\\]*)?"""
    r"""|/;(?:stream\.mp3)?"""
    r"""|/(?:stream|live|listen|radio)[^\s"'<>\\]*"""
    r"""|:\d{4,5}/[^\s"'<>\\]*"""
    r""")""",
    re.IGNORECASE,
)
STREAM_ATTRS = ("src", "data-src", "data-url", "data-stream", "data-audio", "data-mp3", "href")
SKIP_EXT = re.compile(r"\.(?:jpe?g|png|gif|webp|svg|ico|css|js|pdf|zip|woff2?|ttf)(?:\?|$)", re.I)

log = logging.getLogger("tamilradios")


@dataclass
class Station:
    name: str
    page_url: str
    stream_urls: list[str]
    logo: str | None = None
    description: str | None = None
    found_on: list[str] = field(default_factory=list)
    stream_status: dict[str, str] = field(default_factory=dict)


class Crawler:
    def __init__(self, start_url: str, delay: float, max_pages: int, user_agent: str):
        self.start_url = start_url
        self.host = urlparse(start_url).netloc
        self.delay = delay
        self.max_pages = max_pages
        self.session = requests.Session()
        self.session.headers["User-Agent"] = user_agent
        self.robots = RobotFileParser()
        self.robots.set_url(urljoin(start_url, "/robots.txt"))
        try:
            self.robots.read()
        except Exception as e:  # unreachable robots.txt -> be conservative but continue
            log.warning("could not read robots.txt (%s); continuing with delay=%ss", e, delay)
        self.user_agent = user_agent
        self._last_fetch = 0.0

    def allowed(self, url: str) -> bool:
        try:
            return self.robots.can_fetch(self.user_agent, url)
        except Exception:
            return True

    def fetch(self, url: str) -> str | None:
        wait = self.delay - (time.monotonic() - self._last_fetch)
        if wait > 0:
            time.sleep(wait)
        for attempt in range(3):
            self._last_fetch = time.monotonic()
            try:
                r = self.session.get(url, timeout=20)
            except requests.RequestException as e:
                log.warning("fetch %s failed (%s), attempt %d", url, e, attempt + 1)
                time.sleep(2 ** (attempt + 1))
                continue
            if r.status_code in (429, 503):
                time.sleep(int(r.headers.get("Retry-After", 2 ** (attempt + 2))))
                continue
            if r.status_code != 200 or "html" not in r.headers.get("Content-Type", ""):
                return None
            return r.text
        return None

    def normalize(self, base: str, href: str) -> str | None:
        url, _ = urldefrag(urljoin(base, href.strip()))
        p = urlparse(url)
        if p.scheme not in ("http", "https") or p.netloc != self.host or SKIP_EXT.search(p.path):
            return None
        return url

    def crawl(self) -> dict[str, Station]:
        stations: dict[str, Station] = {}
        referrers: dict[str, list[str]] = {}
        queue, seen = deque([self.start_url]), {self.start_url}
        pages = 0
        while queue and pages < self.max_pages:
            url = queue.popleft()
            if not self.allowed(url):
                log.info("robots.txt disallows %s", url)
                continue
            html = self.fetch(url)
            pages += 1
            if html is None:
                continue
            soup = BeautifulSoup(html, "html.parser")
            station = extract_station(url, soup, html)
            if station:
                station.found_on = referrers.get(url, [])
                stations.setdefault(url, station)
                log.info("[%d] station: %s (%d streams)", pages, station.name, len(station.stream_urls))
            for a in soup.find_all("a", href=True):
                link = self.normalize(url, a["href"])
                if not link:
                    continue
                refs = referrers.setdefault(link, [])
                if url not in refs:
                    refs.append(url)
                if link not in seen:
                    seen.add(link)
                    queue.append(link)
        log.info("crawled %d pages, found %d stations", pages, len(stations))
        return stations

    def check_stream(self, url: str) -> str:
        """Read only the response headers to see whether a stream is live."""
        try:
            with self.session.get(url, stream=True, timeout=10) as r:
                ctype = r.headers.get("Content-Type", "?")
                return f"{r.status_code} {ctype}"
        except requests.RequestException as e:
            return f"error: {type(e).__name__}"


def meta(soup: BeautifulSoup, *names: str) -> str | None:
    for n in names:
        tag = soup.find("meta", attrs={"property": n}) or soup.find("meta", attrs={"name": n})
        if tag and tag.get("content"):
            return tag["content"].strip()
    return None


def extract_station(url: str, soup: BeautifulSoup, html: str) -> Station | None:
    streams: list[str] = []

    def add(u: str | None):
        if not u:
            return
        u = urljoin(url, u.strip())
        if u.startswith("http") and not SKIP_EXT.search(urlparse(u).path) and u not in streams:
            streams.append(u)

    for tag in soup.find_all(["audio", "source", "video", "embed", "object", "a"]):
        for attr in STREAM_ATTRS:
            val = tag.get(attr)
            if val and (tag.name in ("audio", "source") or STREAM_RE.fullmatch(urljoin(url, val))):
                add(val)
    for m in STREAM_RE.finditer(html.replace("\\/", "/")):
        add(m.group(0))
    # Ignore links back into the directory site itself; those are navigation, not streams.
    host = urlparse(url).netloc
    streams = [s for s in streams if urlparse(s).netloc != host or re.search(r"\.(mp3|aac|m3u8?|pls)", s, re.I)]
    if not streams:
        return None

    h1 = soup.find("h1")
    name = meta(soup, "og:title") or (h1.get_text(" ", strip=True) if h1 else None) or (
        soup.title.get_text(strip=True) if soup.title else url
    )
    return Station(
        name=name,
        page_url=url,
        stream_urls=streams,
        logo=urljoin(url, logo) if (logo := meta(soup, "og:image", "twitter:image")) else None,
        description=meta(soup, "og:description", "description"),
    )


def write(stations: list[Station], path: str) -> None:
    if path.endswith(".csv"):
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["name", "page_url", "stream_urls", "logo", "description", "found_on", "stream_status"])
            for s in stations:
                w.writerow([s.name, s.page_url, " | ".join(s.stream_urls), s.logo or "", s.description or "",
                            " | ".join(s.found_on), json.dumps(s.stream_status, ensure_ascii=False)])
    else:
        with open(path, "w", encoding="utf-8") as f:
            for s in stations:
                f.write(json.dumps(asdict(s), ensure_ascii=False) + "\n")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--start-url", default=START_URL)
    ap.add_argument("--out", default="stations.jsonl", help=".jsonl or .csv")
    ap.add_argument("--max-pages", type=int, default=500)
    ap.add_argument("--delay", type=float, default=1.5, help="seconds between requests (min 1.0)")
    ap.add_argument("--check-streams", action="store_true", help="probe each stream URL's headers")
    ap.add_argument("--contact", help="email/URL to include in the User-Agent")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(levelname)s %(message)s")
    ua = USER_AGENT.replace("set --contact", args.contact) if args.contact else USER_AGENT
    crawler = Crawler(args.start_url, max(args.delay, 1.0), args.max_pages, ua)
    stations = sorted(crawler.crawl().values(), key=lambda s: s.name.lower())

    if args.check_streams:
        for s in stations:
            for u in s.stream_urls:
                s.stream_status[u] = crawler.check_stream(u)
                time.sleep(0.5)

    write(stations, args.out)
    log.info("wrote %d stations to %s", len(stations), args.out)
    return 0 if stations else 1


if __name__ == "__main__":
    sys.exit(main())
