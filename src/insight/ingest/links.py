"""Fetch a table from a link, safely.

A link is rewritten first (a Google Sheet's editing page becomes its CSV
export, a GitHub file page its raw file). The fetch then refuses private and
internal hosts, follows redirects by hand so a public URL cannot bounce into
an internal one, caps the body size and times out.
"""

from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
from urllib.parse import urljoin, urlparse

import httpx

MAX_BYTES = 50 * 1024 * 1024
MAX_REDIRECTS = 5
TIMEOUT = httpx.Timeout(20.0, connect=5.0)


class LinkError(ValueError):
    pass


def rewrite(url: str) -> str:
    """The URL that serves the data behind a page people share."""
    url = url.strip()
    if m := re.match(r"https://docs\.google\.com/spreadsheets/d/([\w-]+)(?:/[^#?]*)?(?:[?#].*?gid=(\d+))?", url):
        gid = f"&gid={m.group(2)}" if m.group(2) else ""
        return f"https://docs.google.com/spreadsheets/d/{m.group(1)}/export?format=csv{gid}"
    if m := re.match(r"https://github\.com/([^/]+)/([^/]+)/blob/(.+)", url):
        return f"https://raw.githubusercontent.com/{m.group(1)}/{m.group(2)}/{m.group(3)}"
    if "dropbox.com" in url:
        return re.sub(r"([?&])dl=0", r"\1dl=1", url) if "dl=0" in url else url + ("&" if "?" in url else "?") + "dl=1"
    return url


async def _check_host(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise LinkError("Only http and https links can be read.")
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(parsed.hostname, parsed.port or 443, type=socket.SOCK_STREAM)
    except socket.gaierror as e:
        raise LinkError(f"{parsed.hostname} couldn't be found.") from e
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global or ip.is_multicast:
            raise LinkError("Links to private or internal addresses aren't allowed.")


def filename_for(url: str, content_type: str | None) -> str:
    path = urlparse(url).path
    name = path.rsplit("/", 1)[-1] or "download"
    if "." not in name:
        ct = (content_type or "").lower()
        ext = ".json" if "json" in ct else ".xlsx" if "spreadsheet" in ct else ".parquet" if "parquet" in ct else ".csv"
        name += ext
    return name


async def fetch(url: str, client: httpx.AsyncClient | None = None) -> tuple[bytes, str]:
    """Download a link. Returns the body and a filename to read it as."""
    target = rewrite(url)
    own = client is None
    client = client or httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=False, headers={"User-Agent": "InsightBot/1.0"})
    try:
        for _ in range(MAX_REDIRECTS + 1):
            await _check_host(target)
            async with client.stream("GET", target) as resp:
                if resp.is_redirect and "location" in resp.headers:
                    target = urljoin(target, resp.headers["location"])
                    continue
                if resp.status_code >= 400:
                    raise LinkError(f"The link answered with an error ({resp.status_code}).")
                if int(resp.headers.get("content-length") or 0) > MAX_BYTES:
                    raise LinkError("The file behind the link is larger than 50 MB.")
                body = bytearray()
                async for chunk in resp.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > MAX_BYTES:
                        raise LinkError("The file behind the link is larger than 50 MB.")
                return bytes(body), filename_for(target, resp.headers.get("content-type"))
        raise LinkError("The link redirected too many times.")
    except httpx.HTTPError as e:
        raise LinkError(f"The link couldn't be fetched ({type(e).__name__}).") from e
    finally:
        if own:
            await client.aclose()
