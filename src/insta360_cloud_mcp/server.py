"""Insta360 Cloud MCP Server — browse and download media from Insta360 cloud."""

import hashlib
import json
import os
import sys
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

import httpx
from mcp.server.fastmcp import Context, FastMCP

# ── API Endpoints ─────────────────────────────────────────────────────
LOGIN_URL = "https://service-g.insta360.com/infr-user-service/account/service/v1/signin"
SEARCH_LIST_URL = "https://service-sg.insta360.com/cloud/service/media/view/searchList"
DOWNLOAD_URL = "https://service-sg.insta360.com/cloud/service/media/download"
REFRESH_TOKEN_URL = "https://openapi-g.insta360.com/account/v2/refreshToken"
MEDIA_EDIT_MULTI_URL = "https://service-sg.insta360.com/cloud/service/media/edit/multi"

# Cursor meaning "from the very beginning" — the API returns items updated *after*
# this position, so anything higher silently yields an empty list.
INITIAL_CURSOR = "0"

# ── Headers ───────────────────────────────────────────────────────────
CLOUD_HEADERS = {
    "x-insta360-platform": "pc/mac",
    "x-equipment-code": "BE:1B:93:AB:7D:07",
    "referer": "https://cloud.insta360.com/",
    "app_traffic": "release",
    "app_version": "5.9.0",
    "user-agent": "insta360/studio",
    "accept-encoding": "gzip, deflate",
    "accept-language": "en-ID,*",
}

LOGIN_HEADERS = {
    "x-country": "US",
    "accept": "application/json, text/plain, */*",
    "user-agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/102.0.10.2576 Safari/537.36"
    ),
    "content-type": "application/json;charset=UTF-8",
    "origin": "https://www.insta360.com",
    "referer": "https://www.insta360.com/",
    "accept-encoding": "gzip, deflate, br",
    "accept-language": "en-US,en;q=0.9",
}

# ── Paths ─────────────────────────────────────────────────────────────
TOKEN_FILE = Path.home() / ".insta360_tokens.json"
DEFAULT_DOWNLOAD_DIR = Path.home() / "gdrive" / "Insta360"


# ── Utilities ─────────────────────────────────────────────────────────
def _trace_id() -> str:
    return "pc" + uuid.uuid4().hex


def _md5_hash(password: str) -> str:
    return hashlib.md5(password.encode()).hexdigest()


def _extract_token(data: dict, key: str) -> str | None:
    d = data.get("data", {})
    # Only fall back to "token" for access_token lookups, not refresh_token
    result = d.get(key) or data.get(key)
    if not result and key in ("access_token", "token"):
        result = d.get("token")
    return result


# The API answers with HTTP 200 even when auth has failed; the real status lives
# in the JSON body's "code" field. Anything non-zero is an error.
AUTH_ERROR_CODES = (401, 403)


def _api_error_code(data: object) -> int | None:
    if isinstance(data, dict):
        code = data.get("code")
        if isinstance(code, int) and code != 0:
            return code
    return None


def _filename_from_url(url: str) -> str:
    return os.path.basename(urlparse(url).path) or "unknown_file"


def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


# ── Token Persistence ─────────────────────────────────────────────────
def _load_tokens() -> dict | None:
    try:
        data = json.loads(TOKEN_FILE.read_text())
        if data.get("access_token"):
            return data
    except (FileNotFoundError, json.JSONDecodeError, KeyError):
        pass
    return None


def _save_tokens(access_token: str, refresh_token: str | None) -> None:
    TOKEN_FILE.write_text(json.dumps({
        "access_token": access_token,
        "refresh_token": refresh_token,
        "saved_at": time.time(),
        "saved_at_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }, indent=2))


# ── Insta360 API Client ──────────────────────────────────────────────
@dataclass
class Insta360Client:
    email: str
    password_hash: str
    http: httpx.AsyncClient = field(default=None, init=False, repr=False)
    access_token: str | None = field(default=None, init=False)
    refresh_token_value: str | None = field(default=None, init=False)

    async def start(self) -> None:
        self.http = httpx.AsyncClient(headers=CLOUD_HEADERS, timeout=30.0)
        saved = _load_tokens()
        if saved:
            self.access_token = saved["access_token"]
            self.refresh_token_value = saved.get("refresh_token")
            self._apply_token()
            _log("[*] Loaded saved tokens, validating...")
            if await self._validate_token():
                _log("[+] Saved token is valid")
                return
            _log("[-] Saved token invalid, trying refresh...")
            if await self._refresh():
                _log("[+] Token refreshed successfully")
                return
        _log("[*] Performing full login...")
        await self._login()
        _log("[+] Login successful")

    async def stop(self) -> None:
        if self.http:
            await self.http.aclose()

    def _apply_token(self) -> None:
        self.http.headers["authentication"] = self.access_token
        self.http.cookies.set("insta_access_token", self.access_token)

    async def _validate_token(self) -> bool:
        try:
            self.http.headers["x-insta360-trace-id"] = _trace_id()
            resp = await self.http.get(
                SEARCH_LIST_URL,
                params={"lastUpdatePosition": INITIAL_CURSOR},
            )
            if resp.status_code != 200:
                return False
            return _api_error_code(resp.json()) not in AUTH_ERROR_CODES
        except (httpx.HTTPError, ValueError):
            return False

    async def _login(self) -> None:
        payload = {
            "password": self.password_hash,
            "email": self.email,
            "captchaTicket": "",
            "username": self.email,
        }
        resp = await self.http.post(LOGIN_URL, json=payload, headers=LOGIN_HEADERS)
        resp.raise_for_status()
        data = resp.json()

        self.access_token = _extract_token(data, "access_token")
        self.refresh_token_value = _extract_token(data, "refresh_token")

        if not self.access_token:
            raise RuntimeError(f"Login failed — no token in response: {json.dumps(data, indent=2)}")

        self._apply_token()
        _save_tokens(self.access_token, self.refresh_token_value)

    async def _refresh(self) -> bool:
        if not self.access_token:
            return False
        try:
            headers = {
                "content-type": "application/json",
                "x-user-token": self.access_token,
                "x-insta360-trace-id": _trace_id(),
            }
            resp = await self.http.get(REFRESH_TOKEN_URL, headers=headers)
            if resp.status_code != 200:
                return False
            data = resp.json()
            new_token = _extract_token(data, "access_token")
            if not new_token:
                return False
            self.access_token = new_token
            self._apply_token()
            _save_tokens(self.access_token, self.refresh_token_value)
            return True
        except httpx.HTTPError:
            return False

    async def _request_json(
        self,
        method: str,
        url: str,
        params: dict | None = None,
        payload: dict | None = None,
    ) -> dict:
        """Send a request, re-authenticating once if the token is rejected.

        Auth failures arrive two ways: an HTTP 4xx, or an HTTP 200 whose body
        carries code 401/403. Both must trigger a retry, or a stale token
        silently degrades into empty results.
        """
        for attempt in (1, 2):
            self.http.headers["x-insta360-trace-id"] = _trace_id()
            if payload is not None:
                self.http.headers["content-type"] = "application/json"
            resp = await self.http.request(method, url, params=params, json=payload)

            if resp.status_code not in (400, 401, 403):
                resp.raise_for_status()
                try:
                    data = resp.json()
                except ValueError:
                    raise RuntimeError(
                        f"Non-JSON response from {url}: {resp.text[:200]}"
                    ) from None
                code = _api_error_code(data)
                if code is None:
                    return data
                if code not in AUTH_ERROR_CODES:
                    raise RuntimeError(
                        f"Insta360 API error {code} from {url}: {data.get('msg')}"
                    )

            if attempt == 2:
                raise RuntimeError(
                    f"Authentication failed for {url} even after re-login. "
                    f"Check INSTA360_EMAIL/INSTA360_PASSWORD, or delete {TOKEN_FILE}."
                )
            _log("[*] Token rejected, re-authenticating...")
            if not await self._refresh():
                await self._login()

    async def _api_get(self, url: str, params: dict | None = None) -> dict:
        return await self._request_json("GET", url, params=params)

    async def _api_put(self, url: str, payload: dict) -> dict:
        return await self._request_json("PUT", url, payload=payload)

    # ── Public API ────────────────────────────────────────────────

    async def search_list(self, last_update_position: str | None = None) -> dict:
        # lastUpdatePosition is required by the API; "0" starts from the beginning
        cursor = last_update_position or INITIAL_CURSOR
        return await self._api_get(SEARCH_LIST_URL, {"lastUpdatePosition": cursor})

    async def fetch_all_media(self, last_update_position: str | None = None) -> list[dict]:
        all_items: list[dict] = []
        cursor = last_update_position
        page = 0
        while True:
            page += 1
            _log(f"[*] Fetching media page {page} (cursor={cursor})...")
            data = await self.search_list(cursor)
            items = data.get("data", {}).get("list", [])
            if not items:
                break
            all_items.extend(items)
            new_cursor = data.get("data", {}).get("lastUpdatePosition")
            if not new_cursor or new_cursor == cursor:
                break
            cursor = new_cursor
        _log(f"[+] Fetched {len(all_items)} total media items across {page} page(s)")
        return all_items

    async def get_download_urls(self, media_id: str) -> list[str]:
        data = await self._api_get(DOWNLOAD_URL, {"mediaId": media_id})
        return [p["url"] for p in data.get("data", {}).get("paths", []) if p.get("url")]

    async def download_file(self, url: str, dest_dir: Path) -> dict:
        filename = _filename_from_url(url)
        dest = dest_dir / filename
        if dest.exists():
            return {"filename": filename, "status": "skipped", "path": str(dest)}
        dest_dir.mkdir(parents=True, exist_ok=True)
        async with self.http.stream("GET", url) as resp:
            resp.raise_for_status()
            with open(dest, "wb") as f:
                async for chunk in resp.aiter_bytes(chunk_size=8192):
                    f.write(chunk)
        return {"filename": filename, "status": "downloaded", "path": str(dest)}

    async def recycle_media(self, media_ids: list[str]) -> dict:
        return await self._api_put(
            MEDIA_EDIT_MULTI_URL,
            {"action": "to_recycle", "mediaIds": media_ids},
        )


# ── Lifespan & Server ────────────────────────────────────────────────
@dataclass
class AppContext:
    client: Insta360Client


@asynccontextmanager
async def app_lifespan(server: FastMCP) -> AsyncIterator[AppContext]:
    email = os.environ.get("INSTA360_EMAIL", "")
    password = os.environ.get("INSTA360_PASSWORD", "")
    if not email or not password:
        raise RuntimeError(
            "INSTA360_EMAIL and INSTA360_PASSWORD environment variables are required"
        )
    client = Insta360Client(email=email, password_hash=_md5_hash(password))
    await client.start()
    try:
        yield AppContext(client=client)
    finally:
        await client.stop()


mcp = FastMCP("insta360-cloud", lifespan=app_lifespan)


def _client(ctx: Context) -> Insta360Client:
    return ctx.request_context.lifespan_context.client


# ── Tools ─────────────────────────────────────────────────────────────
@mcp.tool()
async def list_media(
    last_update_position: str | None = None,
    ctx: Context = None,
) -> str:
    """Fetch all media from Insta360 cloud, auto-paginating through all pages.

    Args:
        last_update_position: Optional cursor to start pagination from.

    Returns:
        JSON with count and list of media items.
    """
    try:
        items = await _client(ctx).fetch_all_media(last_update_position)
        return json.dumps({"count": len(items), "items": items}, indent=2)
    except Exception as exc:
        return json.dumps({"error": str(exc)})


@mcp.tool()
async def get_download_urls(media_id: str, ctx: Context = None) -> str:
    """Get download URLs for a specific media item.

    Args:
        media_id: The mediaId to get download URLs for.

    Returns:
        JSON with media_id, count, and list of URLs.
    """
    try:
        urls = await _client(ctx).get_download_urls(media_id)
        return json.dumps({"media_id": media_id, "count": len(urls), "urls": urls}, indent=2)
    except Exception as exc:
        return json.dumps({"error": str(exc)})


@mcp.tool()
async def download_media(
    media_id: str,
    download_dir: str | None = None,
    ctx: Context = None,
) -> str:
    """Download all files for a specific media item.

    Args:
        media_id: The mediaId to download.
        download_dir: Directory to save files. Defaults to ~/gdrive/Insta360.

    Returns:
        JSON with download results per file.
    """
    try:
        client = _client(ctx)
        dest = Path(download_dir) if download_dir else DEFAULT_DOWNLOAD_DIR
        urls = await client.get_download_urls(media_id)
        if not urls:
            return json.dumps({"media_id": media_id, "error": "No download URLs found"})
        results = []
        for url in urls:
            result = await client.download_file(url, dest)
            results.append(result)
        return json.dumps({"media_id": media_id, "files": results}, indent=2)
    except Exception as exc:
        return json.dumps({"error": str(exc)})


@mcp.tool()
async def download_all(
    download_dir: str | None = None,
    last_update_position: str | None = None,
    ctx: Context = None,
) -> str:
    """Download all media from Insta360 cloud (auto-paginated).

    Args:
        download_dir: Directory to save files. Defaults to ~/gdrive/Insta360.
        last_update_position: Optional cursor to start pagination from.

    Returns:
        JSON summary with counts of downloaded, skipped, and failed items.
    """
    try:
        client = _client(ctx)
        dest = Path(download_dir) if download_dir else DEFAULT_DOWNLOAD_DIR
        items = await client.fetch_all_media(last_update_position)
        if not items:
            return json.dumps({"message": "No media items found", "count": 0})

        summary = {"total_items": len(items), "downloaded": 0, "skipped": 0, "failed": 0, "errors": []}
        for i, item in enumerate(items, 1):
            media_id = item.get("mediaId")
            if not media_id:
                continue
            _log(f"[{i}/{len(items)}] Processing media {media_id}")
            try:
                urls = await client.get_download_urls(media_id)
                for url in urls:
                    result = await client.download_file(url, dest)
                    if result["status"] == "downloaded":
                        summary["downloaded"] += 1
                    else:
                        summary["skipped"] += 1
            except Exception as exc:
                summary["failed"] += 1
                summary["errors"].append(f"{media_id}: {exc}")
        return json.dumps(summary, indent=2)
    except Exception as exc:
        return json.dumps({"error": str(exc)})


@mcp.tool()
async def delete_media(media_ids: list[str], ctx: Context = None) -> str:
    """Move media items to the recycle bin in Insta360 cloud.

    Args:
        media_ids: List of mediaId strings to recycle.

    Returns:
        JSON with the API response.
    """
    try:
        result = await _client(ctx).recycle_media(media_ids)
        return json.dumps({"recycled": media_ids, "response": result}, indent=2)
    except Exception as exc:
        return json.dumps({"error": str(exc)})


def main():
    mcp.run()


if __name__ == "__main__":
    main()
