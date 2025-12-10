import asyncio
import os
import re
import time
import glob
import shutil
from typing import Union, Tuple, List, Optional, Any
import yt_dlp
from pyrogram.enums import MessageEntityType
from pyrogram.types import Message
from py_yt import VideosSearch
from ShrutiMusic.utils.formatters import time_to_seconds
import aiohttp
from ShrutiMusic import LOGGER

# optional aiofiles for non-blocking file writes; we fall back to sync writes if not present
try:
    import aiofiles  # type: ignore

    _HAVE_AIOFILES = True
except Exception:
    _HAVE_AIOFILES = False

YOUR_API_URL = None
FALLBACK_API_URL = "https://shrutibots.site"

# Cache and retry settings
_CACHE = {}
_CACHE_LOCK = asyncio.Lock()
CACHE_TTL = 600  # seconds (tuned for quicker response + more cache hits)

# Search-specific tuning (make search faster / more responsive)
SEARCH_MAX_RETRIES = 1
SEARCH_RETRY_BACKOFF = 1.2
SEARCH_TIMEOUT = 3  # seconds for VideosSearch next() call (tuned)

# In-flight dedupe: key -> asyncio.Future
_IN_FLIGHT: dict = {}
_IN_FLIGHT_LOCK = asyncio.Lock()

MAX_RETRIES = 3
RETRY_BACKOFF = 1.5  # multiplier

logger = LOGGER("ShrutiMusic.platforms.Youtube.py")

# Environment variable name for cookie file path (set by write_cookies_from_env.py on Heroku)
COOKIE_ENV_VAR = "YTDL_COOKIE_FILE"


def get_cookiefile() -> Optional[str]:
    """
    Return a cookiefile path if available, otherwise None.
    Priority:
     - env YTDL_COOKIE_FILE (recommended)
     - fallback to 'cookies.txt' if present (keeps backward compatibility for local deploys)
    """
    path = os.environ.get(COOKIE_ENV_VAR)
    if path and os.path.isfile(path):
        return path
    # legacy fallback
    if os.path.isfile("cookies.txt"):
        return "cookies.txt"
    # no cookie file available
    logger.debug("No cookie file found via %s and cookies.txt not present", COOKIE_ENV_VAR)
    return None


async def load_api_url():
    global YOUR_API_URL
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get("https://pastebin.com/raw/rLsBhAQa", timeout=aiohttp.ClientTimeout(total=10)) as response:
                if response.status == 200:
                    content = await response.text()
                    YOUR_API_URL = content.strip()
                    logger.info("API URL loaded successfully: %s", YOUR_API_URL)
                else:
                    YOUR_API_URL = FALLBACK_API_URL
                    logger.info("Using fallback API URL (status %s)", response.status)
    except Exception as e:
        YOUR_API_URL = FALLBACK_API_URL
        logger.info("Using fallback API URL due to error: %s", e)


# Try to load api url on import without blocking if there's a running loop
try:
    try:
        # If there is a running loop, schedule a background task
        loop = asyncio.get_running_loop()
        loop.create_task(load_api_url())
    except RuntimeError:
        # No running loop: run loader in a background thread to avoid blocking import
        import threading

        def _run_loader_in_thread():
            try:
                asyncio.run(load_api_url())
            except Exception:
                # swallow exceptions here; load_api_url logs its own errors
                pass

        t = threading.Thread(target=_run_loader_in_thread, daemon=True)
        t.start()
except Exception as e:
    logger.debug("Failed to schedule load_api_url at import-time: %s", e)


# helper to extract video id robustly
def extract_video_id(link: str) -> Union[str, None]:
    """
    Extract a YouTube video id from many possible URL forms and raw ids.

    Returns None if no plausible id found.
    Preference given to 11-character IDs (standard YouTube ID length).
    """
    if not link:
        return None
    link = link.strip()

    # If the entire string is just an 11-char id, return quickly
    if re.fullmatch(r"[A-Za-z0-9_-]{11}", link):
        return link

    # Common patterns to capture IDs (most YouTube ids are 11 chars)
    patterns = [
        r"(?:v=|\/embed\/|\/shorts\/|\/watch\?v=)([A-Za-z0-9_-]{11})",
        r"youtu\.be\/([A-Za-z0-9_-]{11})",
        r"(?:m\.|www\.)?youtube(?:-nocookie)?\.com\/watch\?.*v=([A-Za-z0-9_-]{11})",
        r"(?:m\.|www\.)?youtube(?:-nocookie)?\.com\/embed\/([A-Za-z0-9_-]{11})",
    ]
    for p in patterns:
        m = re.search(p, link)
        if m:
            return m.group(1)

    # fallback: try to capture shorter ids only if 11-char not found (backwards compatibility)
    alt_patterns = [
        r"(?:v=|\/embed\/|\/shorts\/|\/watch\?v=)([A-Za-z0-9_-]{6,10})",
        r"youtu\.be\/([A-Za-z0-9_-]{6,10})",
        r"(?:m\.|www\.)?youtube(?:-nocookie)?\.com\/watch\?.*v=([A-Za-z0-9_-]{6,10})",
        r"(?:m\.|www\.)?youtube(?:-nocookie)?\.com\/embed\/([A-Za-z0-9_-]{6,10})",
    ]
    for p in alt_patterns:
        m = re.search(p, link)
        if m:
            vid = m.group(1)
            logger.debug("Extracted non-standard-length video id: %s", vid)
            return vid

    # fallback: parse query param v=
    if "v=" in link:
        vid = link.split("v=")[-1].split("&")[0]
        if re.fullmatch(r"[A-Za-z0-9_-]{6,11}", vid):
            return vid

    return None


YOUTUBE_URL_RE = re.compile(
    r"(?P<url>https?://(?:www\.|m\.|music\.)?youtube(?:-nocookie)?\.[^\")\s]+|https?://youtu\.be/[A-Za-z0-9_-]{6,})",
    flags=re.IGNORECASE,
)


async def _cached_search(query: str, limit: int = 1) -> List[dict]:
    """
    Cached search with:
      - normalized cache key
      - simplified in-flight dedupe (single network request per key)
      - tight timeout and reduced retries for responsiveness
    """
    if not query:
        return []

    # Normalize key: strip and lowercase to increase cache hits
    normalized_query = " ".join(query.strip().split()).lower()
    key = f"videos:{normalized_query}:{limit}"
    now = time.time()

    # Check cache quickly
    async with _CACHE_LOCK:
        entry = _CACHE.get(key)
        if entry and (now - entry[0]) < CACHE_TTL:
            return entry[1]

    # In-flight dedupe: if another coroutine is already fetching, wait for it
    async with _IN_FLIGHT_LOCK:
        future = _IN_FLIGHT.get(key)
        if future is None:
            future = asyncio.get_event_loop().create_future()
            _IN_FLIGHT[key] = future
            is_fetcher = True
        else:
            is_fetcher = False

    if not is_fetcher:
        # wait for fetcher to complete (with a sensible timeout)
        try:
            result = await asyncio.wait_for(future, timeout=SEARCH_TIMEOUT * (SEARCH_MAX_RETRIES + 1))
            return result or []
        except Exception:
            # If waiter times out or future errored, fall through to attempt our own fetch
            logger.debug("Waiting for in-flight search future failed or timed out for key %s; attempting fetch", key)

    # Only fetcher reaches here to perform network fetch
    results: List[dict] = []
    last_exc = None
    backoff = 1.0
    try:
        for attempt in range(1, SEARCH_MAX_RETRIES + 1):
            try:
                vs = VideosSearch(query, limit=limit)
                try:
                    res = await asyncio.wait_for(vs.next(), timeout=SEARCH_TIMEOUT)
                except asyncio.TimeoutError as te:
                    raise RuntimeError(f"VideosSearch timed out after {SEARCH_TIMEOUT}s") from te
                if isinstance(res, dict):
                    results = res.get("result", []) or []
                else:
                    results = []
                # cache the successful (or empty) result
                async with _CACHE_LOCK:
                    _CACHE[key] = (time.time(), results)
                # fulfill future for waiters
                if not future.done():
                    future.set_result(results)
                return results
            except Exception as e:
                last_exc = e
                logger.warning("VideosSearch attempt %s failed for %s: %s", attempt, query, e)
                if attempt < SEARCH_MAX_RETRIES:
                    await asyncio.sleep(backoff)
                    backoff *= SEARCH_RETRY_BACKOFF
        logger.error("VideosSearch failed after %s attempts for %s: %s", SEARCH_MAX_RETRIES, query, last_exc)
    except Exception as outer:
        logger.exception("Unexpected error in _cached_search: %s", outer)
        last_exc = outer
    finally:
        # ensure future is fulfilled to unblock waiters and cleanup
        async with _IN_FLIGHT_LOCK:
            if not future.done():
                try:
                    future.set_result(results or [])
                except Exception:
                    try:
                        future.set_exception(RuntimeError("failed to set future result"))
                    except Exception:
                        pass
            _IN_FLIGHT.pop(key, None)

    return results or []


async def download_with_retries(session: aiohttp.ClientSession, url: str, dest_path: str, timeout_total=300) -> bool:
    """
    Download a stream URL with retries. Uses aiofiles if available for non-blocking writes.
    """
    last_exc = None
    backoff = 1.0
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=timeout_total)) as resp:
                if resp.status != 200:
                    raise RuntimeError(f"bad response status: {resp.status}")
                # write to a temporary file then move
                tmp_path = dest_path + ".part"
                # Ensure parent dir exists
                os.makedirs(os.path.dirname(dest_path) or ".", exist_ok=True)

                if _HAVE_AIOFILES:
                    # non-blocking write
                    async with aiofiles.open(tmp_path, "wb") as f:
                        async for chunk in resp.content.iter_chunked(16384):
                            if not chunk:
                                continue
                            await f.write(chunk)
                else:
                    # fallback: synchronous write (may block event loop)
                    # we try to minimize blocking by writing in moderately sized chunks
                    with open(tmp_path, "wb") as f:
                        async for chunk in resp.content.iter_chunked(16384):
                            if not chunk:
                                continue
                            f.write(chunk)

                # atomic move
                shutil.move(tmp_path, dest_path)
                return True
        except Exception as e:
            last_exc = e
            logger.warning("Download attempt %s failed for %s: %s", attempt, url, e)
            if attempt < MAX_RETRIES:
                await asyncio.sleep(backoff)
                backoff *= RETRY_BACKOFF
    logger.error("Download failed after %s attempts for %s: %s", MAX_RETRIES, url, last_exc)
    return False


def _yt_dlp_download_sync(video_url: str, download_dir: str, video_id: str, is_audio: bool):
    try:
        outtmpl = os.path.join(download_dir, f"{video_id}.%(ext)s")
        cookiefile = get_cookiefile()
        ydl_opts = {
            "format": "bestaudio/best" if is_audio else "best",
            "outtmpl": outtmpl,
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "restrictfilenames": True,
            "ignoreerrors": False,
        }
        if cookiefile:
            ydl_opts["cookiefile"] = cookiefile
            logger.info("yt_dlp will use cookie file: %s", cookiefile)
        else:
            logger.debug("No cookie file provided; yt_dlp will run unauthenticated")

        if is_audio:
            ydl_opts["postprocessors"] = [
                {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}
            ]
        else:
            ydl_opts["merge_output_format"] = "mp4"

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([video_url])

        patterns = glob.glob(os.path.join(download_dir, f"{video_id}.*"))
        if not patterns:
            return None
        chosen = None
        if is_audio:
            for ext_pref in (".mp3", ".m4a", ".webm", ".opus", ".aac"):
                for p in patterns:
                    if p.lower().endswith(ext_pref):
                        chosen = p
                        break
                if chosen:
                    break
        else:
            for ext_pref in (".mp4", ".mkv", ".webm"):
                for p in patterns:
                    if p.lower().endswith(ext_pref):
                        chosen = p
                        break
                if chosen:
                    break
        if not chosen:
            chosen = patterns[0]
        return chosen
    except Exception as e:
        try:
            logger.exception("yt_dlp sync download error: %s", e)
        except Exception:
            pass
        return None


async def yt_dlp_fallback_download(video_url: str, download_dir: str, video_id: str, is_audio: bool, final_path: str) -> bool:
    loop = asyncio.get_event_loop()
    produced = await loop.run_in_executor(None, _yt_dlp_download_sync, video_url, download_dir, video_id, is_audio)
    if not produced:
        logger.error("yt_dlp fallback failed to produce file for %s", video_url)
        return False
    try:
        if os.path.abspath(produced) != os.path.abspath(final_path):
            if os.path.exists(final_path):
                try:
                    os.remove(final_path)
                except Exception:
                    pass
            shutil.move(produced, final_path)
        return True
    except Exception as e:
        logger.exception("Failed to move yt_dlp produced file %s -> %s: %s", produced, final_path, e)
        return False


async def download_song(link: str) -> Optional[str]:
    global YOUR_API_URL
    if not YOUR_API_URL:
        await load_api_url()
        if not YOUR_API_URL:
            YOUR_API_URL = FALLBACK_API_URL

    video_id = extract_video_id(link)
    if not video_id or len(video_id) < 6:
        return None

    DOWNLOAD_DIR = "downloads"
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    file_path = os.path.join(DOWNLOAD_DIR, f"{video_id}.mp3")

    if os.path.exists(file_path):
        return file_path

    tried_api = False
    try:
        async with aiohttp.ClientSession() as session:
            params = {"url": video_id, "type": "audio"}
            last_exc = None
            backoff = 1.0
            for attempt in range(1, MAX_RETRIES + 1):
                try:
                    tried_api = True
                    async with session.get(f"{YOUR_API_URL}/download", params=params, timeout=aiohttp.ClientTimeout(total=30)) as response:
                        if response.status != 200:
                            raise RuntimeError(f"downloader api returned {response.status}")
                        data = await response.json()
                        stream_url = data.get("stream_url")
                        if not stream_url:
                            raise RuntimeError("no stream_url in api response")
                        ok = await download_with_retries(session, stream_url, file_path, timeout_total=300)
                        if ok:
                            return file_path
                        else:
                            raise RuntimeError("stream download failed")
                except Exception as e:
                    last_exc = e
                    logger.warning("download_song attempt %s failed for %s: %s", attempt, link, e)
                    if attempt < MAX_RETRIES:
                        await asyncio.sleep(backoff)
                        backoff *= RETRY_BACKOFF
            logger.error("download_song API method failed for %s after retries: %s", link, last_exc)
    except Exception as e:
        logger.exception("download_song unexpected error while using API: %s", e)

    logger.info("Attempting yt_dlp fallback for audio: %s", link)
    video_url = link if ("youtube" in link or "youtu.be" in link) else f"https://www.youtube.com/watch?v={video_id}"
    try:
        ok = await yt_dlp_fallback_download(video_url, DOWNLOAD_DIR, video_id, True, file_path)
        if ok and os.path.exists(file_path):
            logger.info("yt_dlp fallback succeeded for audio: %s", video_url)
            return file_path
        else:
            logger.error("yt_dlp fallback failed for audio: %s", video_url)
            return None
    except Exception as e:
        logger.exception("yt_dlp fallback unexpected error for audio %s: %s", link, e)
        return None


async def download_video(link: str) -> Optional[str]:
    global YOUR_API_URL
    if not YOUR_API_URL:
        await load_api_url()
        if not YOUR_API_URL:
            YOUR_API_URL = FALLBACK_API_URL

    video_id = extract_video_id(link)
    if not video_id or len(video_id) < 6:
        return None

    DOWNLOAD_DIR = "downloads"
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    file_path = os.path.join(DOWNLOAD_DIR, f"{video_id}.mp4")

    if os.path.exists(file_path):
        return file_path

    tried_api = False
    try:
        async with aiohttp.ClientSession() as session:
            params = {"url": video_id, "type": "video"}
            last_exc = None
            backoff = 1.0
            for attempt in range(1, MAX_RETRIES + 1):
                try:
                    tried_api = True
                    async with session.get(f"{YOUR_API_URL}/download", params=params, timeout=aiohttp.ClientTimeout(total=30)) as response:
                        if response.status != 200:
                            raise RuntimeError(f"downloader api returned {response.status}")
                        data = await response.json()
                        stream_url = data.get("stream_url")
                        if not stream_url:
                            raise RuntimeError("no stream_url in api response")
                        ok = await download_with_retries(session, stream_url, file_path, timeout_total=600)
                        if ok:
                            return file_path
                        else:
                            raise RuntimeError("stream download failed")
                except Exception as e:
                    last_exc = e
                    logger.warning("download_video attempt %s failed for %s: %s", attempt, link, e)
                    if attempt < MAX_RETRIES:
                        await asyncio.sleep(backoff)
                        backoff *= RETRY_BACKOFF
            logger.error("download_video API method failed for %s after retries: %s", link, last_exc)
    except Exception as e:
        logger.exception("download_video unexpected error while using API: %s", e)

    logger.info("Attempting yt_dlp fallback for video: %s", link)
    video_url = link if ("youtube" in link or "youtu.be" in link) else f"https://www.youtube.com/watch?v={video_id}"
    try:
        ok = await yt_dlp_fallback_download(video_url, DOWNLOAD_DIR, video_id, False, file_path)
        if ok and os.path.exists(file_path):
            logger.info("yt_dlp fallback succeeded for video: %s", video_url)
            return file_path
        else:
            logger.error("yt_dlp fallback failed for video: %s", video_url)
            return None
    except Exception as e:
        logger.exception("yt_dlp fallback unexpected error for video %s: %s", link, e)
        return None


async def shell_cmd(cmd: str) -> str:
    proc = await asyncio.create_subprocess_shell(
        cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    out_dec = out.decode("utf-8", errors="ignore") if out else ""
    err_dec = err.decode("utf-8", errors="ignore") if err else ""
    # if yt-dlp prints a specific stderr we still may want stdout
    if err_dec:
        # special-case common message where stdout contains relevant output
        if "unavailable videos are hidden" in err_dec.lower():
            return out_dec
        # otherwise return both to help debugging
        combined = out_dec + ("\n[stderr]\n" + err_dec if err_dec else "")
        return combined
    return out_dec


class YouTubeAPI:
    def __init__(self):
        self.base = "https://www.youtube.com/watch?v="
        self.regex = r"(?:youtube\\.com|youtu\\.be)"
        self.status = "https://www.youtube.com/oembed?url="
        self.listbase = "https://youtube.com/playlist?list="
        self.reg = re.compile(r"\\x1B(?:[@-Z\\\\-_]|\\[[0-?]*[ -/] *[@-~])")

    async def exists(self, link: str, videoid: Union[bool, str] = None) -> bool:
        if videoid:
            link = self.base + link
        return bool(re.search(self.regex, link))

    async def url(self, message_1: Message) -> Union[str, None]:
        messages = [message_1]
        if message_1.reply_to_message:
            messages.append(message_1.reply_to_message)
        for message in messages:
            if message.entities:
                for entity in message.entities:
                    if entity.type == MessageEntityType.URL:
                        text = message.text or message.caption or ""
                        return text[entity.offset: entity.offset + entity.length]
                    if entity.type == MessageEntityType.TEXT_LINK:
                        return entity.url
            if getattr(message, "caption_entities", None):
                for entity in message.caption_entities:
                    if entity.type == MessageEntityType.TEXT_LINK:
                        return entity.url
            text_content = (message.text or "") + " \n " + (message.caption or "")
            for m in YOUTUBE_URL_RE.finditer(text_content):
                candidate = m.group("url")
                if candidate:
                    return candidate
        return None

    async def details(self, link: str, videoid: Union[bool, str] = None) -> Tuple[Optional[str], Optional[str], int, Optional[str], Optional[str]]:
        if videoid:
            link = self.base + link
        if "&" in link:
            link = link.split("&")[0]
        results = await _cached_search(link, limit=1)
        if not results:
            return None, None, 0, None, None
        r = results[0]
        title = r.get("title")
        duration_min = r.get("duration")
        thumbnail = r.get("thumbnails", [{"url": None}])[0].get("url", "").split("?")[0]
        vidid = r.get("id")
        duration_sec = int(time_to_seconds(duration_min)) if duration_min else 0
        return title, duration_min, duration_sec, thumbnail, vidid

    async def title(self, link: str, videoid: Union[bool, str] = None) -> Optional[str]:
        if videoid:
            link = self.base + link
        if "&" in link:
            link = link.split("&")[0]
        results = await _cached_search(link, limit=1)
        if not results:
            return None
        return results[0].get("title")

    async def duration(self, link: str, videoid: Union[bool, str] = None) -> Optional[str]:
        if videoid:
            link = self.base + link
        if "&" in link:
            link = link.split("&")[0]
        results = await _cached_search(link, limit=1)
        if not results:
            return None
        return results[0].get("duration")

    async def thumbnail(self, link: str, videoid: Union[bool, str] = None) -> Optional[str]:
        if videoid:
            link = self.base + link
        if "&" in link:
            link = link.split("&")[0]
        results = await _cached_search(link, limit=1)
        if not results:
            return None
        return results[0].get("thumbnails", [{}])[0].get("url", "").split("?")[0]

    async def video(self, link: str, videoid: Union[bool, str] = None) -> Tuple[int, Union[str, None]]:
        if videoid:
            link = self.base + link
        if "&" in link:
            link = link.split("&")[0]
        try:
            downloaded_file = await download_video(link)
            if downloaded_file:
                return 1, downloaded_file
            else:
                return 0, "Video download failed"
        except Exception as e:
            logger.exception("video download error: %s", e)
            return 0, f"Video download error: {e}"

    async def playlist(self, link: str, limit: int, user_id: int, videoid: Union[bool, str] = None) -> List[str]:
        """
        Use yt_dlp programmatic API to extract flat playlist IDs safely (no shell).
        """
        if videoid:
            link = self.listbase + link
        if "&" in link:
            link = link.split("&")[0]

        cookiefile = get_cookiefile()
        # run extraction in executor to avoid blocking event loop
        def _extract_playlist_ids(url: str, limit_inner: int, cookiefile_inner: Optional[str]) -> List[str]:
            opts: dict = {"quiet": True, "skip_download": True, "extract_flat": True}
            if cookiefile_inner:
                opts["cookiefile"] = cookiefile_inner
            try:
                with yt_dlp.YoutubeDL(opts) as ydl_local:
                    info = ydl_local.extract_info(url, download=False)
                    entries = []
                    if isinstance(info, dict) and info.get("entries"):
                        for e in info.get("entries", []):
                            if not e:
                                continue
                            # entries in flat mode may be dicts with 'id'
                            if isinstance(e, dict):
                                ent_id = e.get("id")
                                if ent_id:
                                    entries.append(ent_id)
                            elif isinstance(e, str):
                                entries.append(e)
                            if len(entries) >= limit_inner:
                                break
                    return entries
            except Exception as ex:
                logger.exception("playlist extraction failed for %s: %s", url, ex)
                return []

        loop = asyncio.get_event_loop()
        ids = await loop.run_in_executor(None, _extract_playlist_ids, link, limit, cookiefile)
        return ids

    async def track(self, link: str, videoid: Union[bool, str] = None) -> Tuple[dict, Optional[str]]:
        if videoid:
            link = self.base + link
        if "&" in link:
            link = link.split("&")[0]
        results = await _cached_search(link, limit=1)
        if not results:
            return {}, None
        r = results[0]
        title = r.get("title")
        duration_min = r.get("duration")
        vidid = r.get("id")
        yturl = r.get("link")
        thumbnail = r.get("thumbnails", [{"url": None}])[0].get("url", "").split("?")[0]
        track_details = {
            "title": title,
            "link": yturl,
            "vidid": vidid,
            "duration_min": duration_min,
            "thumb": thumbnail,
        }
        return track_details, vidid

    async def formats(self, link: str, videoid: Union[bool, str] = None) -> Tuple[List[dict], str]:
        """
        Run yt_dlp.extract_info in executor to avoid blocking the event loop.
        """
        if videoid:
            link = self.base + link
        if "&" in link:
            link = link.split("&")[0]
        cookiefile = get_cookiefile()

        def _extract_info_sync(url: str, cookiefile_inner: Optional[str]) -> Any:
            ytdl_opts = {"quiet": True}
            if cookiefile_inner:
                ytdl_opts["cookiefile"] = cookiefile_inner
            try:
                with yt_dlp.YoutubeDL(ytdl_opts) as ydl_local:
                    return ydl_local.extract_info(url, download=False)
            except Exception as e:
                logger.exception("formats() extract_info failed in executor: %s", e)
                return None

        loop = asyncio.get_event_loop()
        r = await loop.run_in_executor(None, _extract_info_sync, link, cookiefile)
        formats_available: List[dict] = []
        try:
            # If r is a playlist, pick first entry
            if isinstance(r, dict) and r.get("entries"):
                entries = [e for e in r.get("entries") if e]
                if entries:
                    r = entries[0]
            for fmt in (r.get("formats", []) if r else []):
                try:
                    if "dash" not in str(fmt.get("format", "")).lower():
                        formats_available.append(
                            {
                                "format": fmt.get("format"),
                                "filesize": fmt.get("filesize"),
                                "format_id": fmt.get("format_id"),
                                "ext": fmt.get("ext"),
                                "format_note": fmt.get("format_note"),
                                "yturl": link,
                            }
                        )
                except Exception:
                    continue
        except Exception as e:
            logger.exception("formats() processing failed: %s", e)
        return formats_available, link

    async def slider(self, link: str, query_type: int, videoid: Union[bool, str] = None):
        if videoid:
            link = self.base + link
        if "&" in link:
            link = link.split("&")[0]
        results = await _cached_search(link, limit=10)
        if not results or query_type >= len(results):
            return None, None, None, None
        r = results[query_type]
        title = r.get("title")
        duration_min = r.get("duration")
        vidid = r.get("id")
        thumbnail = r.get("thumbnails", [{"url": None}])[0].get("url", "").split("?")[0]
        return title, duration_min, thumbnail, vidid

    async def download(
        self,
        link: str,
        mystic,
        video: Union[bool, str] = None,
        videoid: Union[bool, str] = None,
        songaudio: Union[bool, str] = None,
        songvideo: Union[bool, str] = None,
        format_id: Union[bool, str] = None,
        title: Union[bool, str] = None,
    ) -> Tuple[Optional[str], bool]:
        if videoid:
            link = self.base + link

        try:
            if video:
                downloaded_file = await download_video(link)
            else:
                downloaded_file = await download_song(link)

            if downloaded_file:
                return downloaded_file, True
            else:
                return None, False
        except Exception as e:
            logger.exception("download error: %s", e)
            return None, False
