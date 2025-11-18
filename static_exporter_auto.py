#!/usr/bin/env python3
"""
static_exporter_auto.py

Non-destructive, fast static exporter for a local WordPress site.

Features:
- Auto-detect base URL by probing common local URLs
- Async crawler using asyncio + aiohttp + aiofiles
- 50 parallel workers (Semaphore)
- Domain filtering + URL normalization
- Retry logic with exponential backoff
- Sitemap preload (if /sitemap.xml exists)
- Logs every fetched and saved URL
- Exports to `static-build` directory (never touches WordPress files)

Usage: python static_exporter_auto.py

This script purposely does not delete or modify any WordPress files.
It only performs HTTP GET requests against the site and writes files
into the `static-build` directory in the workspace.
"""

import asyncio
import logging
import os
import re
import sys
import time
from pathlib import Path
from typing import Set, List

import aiofiles
import aiohttp
from aiohttp import ClientConnectorError, ClientResponseError
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse, urlunparse, urldefrag, urlsplit, urlunsplit, parse_qsl, urlencode

# Configuration
CONCURRENCY = 50
REQUEST_TIMEOUT = 20
RETRIES = 3
BACKOFF_FACTOR = 0.5
OUTPUT_DIR = Path("static-build")
# Probe URLs to auto-detect base URL (order matters)
PROBE_URLS = [
    "http://localhost/wordpress1/",
    "http://localhost/wordpress1/index.php",
    "http://localhost/wordpress1/wp/",
]

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("static_exporter")


def normalize_url(url: str) -> str:
    """Normalize a URL: remove fragment, sort query params, ensure scheme and netloc lowercased."""
    url, _ = urldefrag(url)
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    netloc = parts.netloc.lower()
    query = parts.query
    if query:
        qs = sorted(parse_qsl(query, keep_blank_values=True))
        query = urlencode(qs)
    normalized = urlunsplit((scheme, netloc, parts.path or '/', query, ''))
    return normalized


async def probe_base_url(session: aiohttp.ClientSession, workspace_folder: Path) -> str:
    """Probe common local URLs and return first that returns HTTP 200."""
    # include workspace-folder-derived guess if it looks like a web-root name
    try_urls = list(PROBE_URLS)
    # Heuristic: if workspace folder name looks like site dir (contains 'wordpress' or 'wp')
    name = workspace_folder.name.lower()
    if 'wordpress' in name or name.startswith('wp') or name.endswith('wordpress1'):
        # try a path-derived url
        try_urls.append(f"http://localhost/{workspace_folder.name}/")

    for url in try_urls:
        try:
            async with session.get(url, timeout=REQUEST_TIMEOUT) as resp:
                if resp.status == 200:
                    logger.info(f"Detected base URL: {url}")
                    return normalize_url(str(resp.url))
        except Exception:
            logger.debug(f"Probe failed for {url}", exc_info=True)

    # As a last resort, try http://localhost/ (often used in XAMPP)
    fallback = "http://localhost/"
    try:
        async with session.get(fallback, timeout=REQUEST_TIMEOUT) as resp:
            if resp.status == 200:
                logger.info(f"Falling back to {fallback}")
                return normalize_url(str(resp.url))
    except Exception:
        pass

    raise RuntimeError("Could not detect a working local WordPress URL. Start your local server and try again.")


async def fetch_with_retries(session: aiohttp.ClientSession, url: str, retries: int = RETRIES) -> bytes:
    attempt = 0
    backoff = BACKOFF_FACTOR
    while True:
        try:
            async with session.get(url, timeout=REQUEST_TIMEOUT) as resp:
                resp.raise_for_status()
                data = await resp.read()
                logger.info(f"Fetched: {url} -> {len(data)} bytes")
                return data
        except (asyncio.TimeoutError, ClientConnectorError, ClientResponseError, aiohttp.ClientError) as exc:
            attempt += 1
            if attempt > retries:
                logger.warning(f"Giving up {url} after {attempt} attempts: {exc}")
                raise
            else:
                logger.debug(f"Retrying {url} in {backoff}s (attempt {attempt})")
                await asyncio.sleep(backoff)
                backoff *= 2


def extract_links(base_url: str, html: bytes) -> Set[str]:
    """Extract and normalize links from HTML content, returning absolute URLs filtered to same domain."""
    links: Set[str] = set()
    try:
        soup = BeautifulSoup(html, "html.parser")
        for tag, attr in (("a", "href"), ("link", "href"), ("script", "src"), ("img", "src")):
            for node in soup.find_all(tag):
                val = node.get(attr)
                if not val:
                    continue
                joined = urljoin(base_url, val)
                norm = normalize_url(joined)
                links.add(norm)
    except Exception:
        logger.debug("Failed to parse HTML for links", exc_info=True)
    return links


def url_to_path(base_netloc: str, url: str) -> Path:
    """Map a URL to a file path under OUTPUT_DIR.

    Examples:
    - http://site/ -> static-build/index.html
    - http://site/about/ -> static-build/about/index.html
    - http://site/wp-content/image.jpg -> static-build/wp-content/image.jpg
    - http://site/page.php?id=2 -> static-build/page.php (query discarded by normalization)
    """
    parsed = urlparse(url)
    # drop query in file mapping; keep path
    path = parsed.path
    if path.endswith('/') or path == '':
        # directory -> index.html
        target = OUTPUT_DIR / parsed.netloc / path.lstrip('/') / 'index.html'
    else:
        target = OUTPUT_DIR / parsed.netloc / path.lstrip('/')
    return target


async def save_bytes(path: Path, data: bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = 'wb'
    async with aiofiles.open(path, mode) as f:
        await f.write(data)
    logger.info(f"Saved: {path}")


async def preload_sitemap(session: aiohttp.ClientSession, base_url: str) -> List[str]:
    sitemap_urls = []
    sitemap_variants = ["/sitemap.xml", "/sitemap_index.xml"]
    parsed = urlparse(base_url)
    root = f"{parsed.scheme}://{parsed.netloc}"
    for s in sitemap_variants:
        url = urljoin(root, s)
        try:
            async with session.get(url, timeout=REQUEST_TIMEOUT) as resp:
                if resp.status == 200:
                    text = await resp.text()
                    # find <loc> entries
                    soup = BeautifulSoup(text, 'xml')
                    for loc in soup.find_all('loc'):
                        if loc.string:
                            sitemap_urls.append(normalize_url(loc.string.strip()))
                    if sitemap_urls:
                        logger.info(f"Loaded {len(sitemap_urls)} URLs from sitemap: {url}")
                        return sitemap_urls
        except Exception:
            continue
    return sitemap_urls


def is_wp_content_url(path: str) -> bool:
    """Return True if the path looks like a WordPress post/page/category/tag listing.

    Heuristics used:
    - allow root (/) and paginated roots like /page/2/
    - allow YYYY/MM/DD/... permalinks
    - allow paths containing /category/ or /tag/
    - allow paths that look like pretty permalinks (no file extension)
    - disallow admin, login, wp-json, wp-content, wp-includes, xmlrpc, feed endpoints
    """
    p = path.lower()
    # disallowed prefixes
    disallow = ('/wp-admin', '/wp-login.php', '/wp-json', '/wp-content', '/wp-includes', '/xmlrpc.php')
    if any(p.startswith(d) for d in disallow):
        return False
    # disallow feeds and query search
    if p.startswith('/feed') or p.startswith('/?s') or 's=' in p:
        return False
    # allow categories/tags
    if '/category/' in p or '/tag/' in p:
        return True
    # allow author listings
    if '/author/' in p:
        return True
    # allow date-based permalinks like /2023/05/01/
    if re.match(r'^/\d{4}/\d{1,2}/\d{1,2}/', p):
        return True
    # allow paginated pages like /page/2/
    if re.match(r'^/page/\d+/?$', p):
        return True
    # allow root or paths without extensions (pretty permalinks)
    if p == '/' or ('.' not in os.path.basename(p)):
        # exclude directory names that are clearly assets
        if any(p.endswith(ext) for ext in ('.css', '.js', '.png', '.jpg', '.jpeg', '.svg', '.gif', '.ico', '.webp', '.woff', '.woff2', '.ttf', '.map', '.pdf', '.zip')):
            return False
        return True
    return False


async def crawl(base_url: str):
    parsed = urlparse(base_url)
    base_netloc = parsed.netloc
    # Determine an allowed path prefix to avoid crawling unrelated localhost content.
    # If the detected base_url path contains a subpath (like /wordpress1/), use that.
    # Otherwise, attempt to use the current workspace folder name as a prefix.
    allowed_prefix = parsed.path
    if not allowed_prefix or allowed_prefix == '/':
        # try workspace folder name guess
        ws_guess = Path.cwd().name
        if ws_guess:
            allowed_prefix = '/' + ws_guess.strip('/') + '/'
        else:
            allowed_prefix = '/'
    # Ensure it starts and ends with '/'
    if not allowed_prefix.startswith('/'):
        allowed_prefix = '/' + allowed_prefix
    if not allowed_prefix.endswith('/'):
        allowed_prefix = allowed_prefix + '/'
    logger.info(f"Using allowed path prefix: {allowed_prefix}")
    seen: Set[str] = set()
    to_crawl = asyncio.Queue()

    async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(limit=0)) as session:
        # Preload sitemap URLs if available
        sitemap_urls = await preload_sitemap(session, base_url)
        # If sitemap present, prefer sitemap as seeds (it contains canonical posts/pages/categories/tags)
        seeds = sitemap_urls or [base_url]

        for u in seeds:
            await to_crawl.put(u)

        sem = asyncio.Semaphore(CONCURRENCY)
        crawled_count = 0

        async def worker():
            nonlocal crawled_count
            while True:
                url = await to_crawl.get()
                if url in seen:
                    to_crawl.task_done()
                    continue
                seen.add(url)
                # domain filter
                p = urlparse(url)
                if p.netloc != base_netloc:
                    to_crawl.task_done()
                    continue

                # path prefix filter: only crawl URLs under the allowed_prefix unless they came from sitemap
                if sitemap_urls and url in sitemap_urls:
                    allowed_by_sitemap = True
                else:
                    allowed_by_sitemap = False

                if not allowed_by_sitemap and not p.path.startswith(allowed_prefix):
                    logger.debug(f"Skipping outside-prefix URL: {url}")
                    to_crawl.task_done()
                    continue

                # avoid fetching assets (images, css, js, fonts, etc.) — we only want posts/pages/categories/tags
                asset_exts = ('.css', '.js', '.png', '.jpg', '.jpeg', '.svg', '.gif', '.ico', '.webp', '.woff', '.woff2', '.ttf', '.eot', '.otf', '.map', '.pdf', '.zip')
                if any(p.path.lower().endswith(ext) for ext in asset_exts):
                    logger.debug(f"Skipping asset URL: {url}")
                    to_crawl.task_done()
                    continue

                try:
                    async with sem:
                        data = await fetch_with_retries(session, url)
                except Exception:
                    logger.debug(f"Failed to fetch {url}", exc_info=True)
                    to_crawl.task_done()
                    continue

                # Save the response
                file_path = url_to_path(base_netloc, url)
                try:
                    await save_bytes(file_path, data)
                except Exception:
                    logger.warning(f"Failed to save {file_path}", exc_info=True)

                crawled_count += 1

                # If content looks like HTML, extract more links
                content_type = None
                # naive detection using file extension or HTML markers
                if b"<html" in data[:1000].lower() or re.search(b"<\s*!?doctype", data[:1000].lower()):
                    content_type = 'html'

                if content_type == 'html':
                    try:
                        links = extract_links(url, data)
                        for link in links:
                            # Only queue links that are same-netloc and either in sitemap or start with allowed_prefix
                            lp = urlparse(link)
                            if lp.netloc != base_netloc:
                                continue
                            if lp.path.lower().endswith(asset_exts):
                                continue
                            if sitemap_urls and link in sitemap_urls:
                                if link not in seen:
                                    await to_crawl.put(link)
                            elif lp.path.startswith(allowed_prefix):
                                if link not in seen:
                                    await to_crawl.put(link)
                    except Exception:
                        logger.debug(f"Failed to extract links from {url}", exc_info=True)

                to_crawl.task_done()

        # start workers
        workers = [asyncio.create_task(worker()) for _ in range(CONCURRENCY)]
        await to_crawl.join()
        for w in workers:
            w.cancel()
        # best-effort wait
        await asyncio.gather(*workers, return_exceptions=True)

    return len(seen)


async def main():
    start = time.time()
    workspace = Path.cwd()

    # Ensure output dir exists and is separate from workspace WordPress files
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    async with aiohttp.ClientSession() as session:
        base_url = await probe_base_url(session, workspace)

    # Normalize base_url to root level (scheme + netloc + '/')
    p = urlparse(base_url)
    base_root = f"{p.scheme}://{p.netloc}/"

    try:
        total = await crawl(base_root)
    except Exception as e:
        logger.error(f"Export failed: {e}")
        sys.exit(2)

    duration = time.time() - start
    logger.info(f"Export complete: {total} URLs crawled in {duration:.1f}s")
    print(f"TOTAL_URLS_CRAWLED:{total}")
    print(f"EXPORT_DURATION_SECONDS:{duration:.2f}")


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Cancelled by user")
        sys.exit(1)
