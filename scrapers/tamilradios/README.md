# Tamil radio station directory scraper

Builds a directory of the stations listed on https://www.tamilradios.com/.
It collects metadata only and never records or downloads broadcasts.

```bash
pip install -r requirements.txt
python scrape.py --out stations.jsonl                 # or --out stations.csv
python scrape.py --out stations.csv --check-streams   # also probes each stream's headers
```

Options: `--max-pages` (default 1000), `--delay` seconds between requests (default 1.5, minimum 1),
`--timeout` (default 60), `--contact` (adds an email/URL to the User-Agent), `--start-url`, `-v`.

## How it works

1. Reads `sitemap.xml` to get every page URL. If there is no sitemap, it crawls same-host links instead.
2. For each single-segment page (`/hello-fm-tamil-radio`), it merges two sources embedded in the HTML:
   - the Next.js page payload (`self.__next_f.push(...)`), from which it takes the `"station": {...}`
     object whose `slug` matches the page: stream URL, site proxy URL, bitrate, genre, image, id
   - the schema.org `RadioStation` JSON-LD block: description, frequency, city/region/country,
     language, founding year, official website and social links
3. Each station is appended to `<out>.progress.jsonl` as it is scraped; rerunning the same
   command resumes from there. The file is removed once the final output is written.
4. Pages with neither source (contact, terms, genre listings) are skipped.

## Output fields

`name, slug, page_url, stream_url, web_stream_url, bitrate, genre, frequency, city, region,
country, language, founded, description, image, website_links, station_id, is_active`
(+ `stream_status` with `--check-streams`). The CSV omits `station_id` and `is_active`.

`stream_url` is the station's direct stream; `web_stream_url` is the site's own signed proxy
URL, which may expire.

## Politeness

Obeys robots.txt (the site disallows `/api/`, `/admin/` and others), stays on the same host,
waits between requests, and backs off on 429/503.
