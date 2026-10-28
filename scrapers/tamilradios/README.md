# Tamil radio station directory scraper

Crawls https://www.tamilradios.com/ and writes one record per station page:
`name`, `page_url`, `stream_urls`, `logo`, `description`, `found_on` (the listing pages
that link to it), and optionally `stream_status`.

It collects metadata only and never records or downloads broadcasts.

```bash
pip install -r requirements.txt
python scrape.py --out stations.jsonl                 # or --out stations.csv
python scrape.py --out stations.csv --check-streams   # also probes each stream's headers
```

Options: `--max-pages` (default 500), `--delay` seconds between requests (default 1.5, minimum 1),
`--contact` (adds an email/URL to the User-Agent), `--start-url`, `-v`.

Politeness: obeys robots.txt, stays on the same host, rate-limits requests, and backs off on 429/503.

A page counts as a station page when it contains a stream URL: `<audio>`/`<source>` tags,
`data-*` attributes, or stream-like URLs in inline JS (`.mp3`, `.aac`, `.m3u8`, `.pls`,
Shoutcast `/;`, `:port/...`). If the site loads streams through an API instead, adjust
`extract_station()` in `scrape.py`.
