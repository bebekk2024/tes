import asyncio
import os
import re
import time
import glob
import shutil
from typing import Union
import yt_dlp
from pyrogram.enums import MessageEntityType
from pyrogram.types import Message
from py_yt import VideosSearch
from ShrutiMusic.utils.formatters import time_to_seconds
import aiohttp
from ShrutiMusic import LOGGER

YOUR_API_URL = None
FALLBACK_API_URL = "https://shrutibots.site"

# Cache and retry settings
_CACHE = {}
_CACHE_LOCK = asyncio.Lock()
# Deduplicate concurrent identical requests
_PENDING = {}
_PENDING_LOCK = asyncio.Lock()
CACHE_TTL = 300  # seconds (increase for longer caching)
MAX_RETRIES = 3
RETRY_BACKOFF = 1.5  # multiplier

# Limit concurrent external search/download ops to avoid overload
_CONCURRENCY_LIMIT = 6
_CONCURRENCY_SEMAPHORE = asyncio.Semaphore(_CONCURRENCY_LIMIT)

logger = LOGGER("ShrutiMusic.platforms.Youtube.py")

# Environment variable name for cookie file path (set by write_cookies_from_env.py on Heroku)
COOKIE_ENV_VAR = "YTDL_COOKIE_FILE"

# Reusable aiohttp session
_SESSION: aiohttp.ClientSession = None
_SESSION_LOCK = asyncio.Lock()


async def _get_session() -> aiohttp.ClientSession:
    global _SESSION
    async with _SESSION_LOCK:
        if _SESSION and not _SESSION.closed:
            return _SESSION
        # Configure connector limits to keep many simultaneous requests fast
        connector = aiohttp.TCPConnector(limit=30)
        _SESSION = aiohttp.ClientSession(connector=connector)
        return _SESSION


def get_cookiefile():
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
        session = await _get_session()
        async with session.get("https://pastebin.com/raw/rLsBhAQa", timeout=aiohttp.ClientTimeout(total=10)) as response:
            if response.status == 200:
                content = await response.text()
                YOUR_API_URL = content.strip()
                logger.info("API URL loaded successfully: %s", YOUR_API_URL)
            else:
                YOUR_API_URL = FALLBACK_API_URL
                logger.info("Using fallback API URL")
    except Exception as e:
        YOUR_API_URL = FALLBACK_API_URL
        logger.info("Using fallback API URL due to error: %s", e)


# Try to load api url on import without blocking if there's a running loop
try:
    loop = asyncio.get_event_loop()
    if loop.is_running():
        asyncio.create_task(load_api_url())
    else:
        loop.run_until_complete(load_api_url())
except RuntimeError:
    pass


# helper to extract video id robustly
def extract_video_id(link: str) -> Union[str, None]:
    if not link:
        return None
    link = link.strip()
    patterns = [
        r"(?:v=|\/embed\/|\/shorts\/|\/watch\?v=)([A-Za-z0-9_-]{6,})",
        r"youtu\.be\/([A-Za-z0-9_-]{6,})",
        r"(?:m\.|www\.)?youtube(?:-nocookie)?\.com\/watch\?.*v=([A-Za-z0-9_-]{6,})",
        r"(?:m\.|www\.)?youtube(?:-nocookie)?\.com\/embed\/([A-Za-z0-9_-]{6,})",
    ]
    for p in patterns:
        m = re.search(p, link)
        if m:
            return m.group(1)
    if 'v=' in link:
        vid = link.split('v=')[-1].split('&')[0]
        if re.fullmatch(r"[A-Za-z0-9_-]{6,}", vid):
            return vid
    if re.fullmatch(r"[A-Za-z0-9_-]{6,}", link):
        return link
    return None


YOUTUBE_URL_RE = re.compile(
    r"(?P<url>https?://(?:www\.|m\.|music\.|)youtube(?:-nocookie)?\.[^\")\s]+|https?://youtu\.be/[A-Za-z0-9_-]{6,})",
    flags=re.IGNORECASE,
)


async def _cached_search(query: str, limit: int = 1):
    """
    Fast cached search:
    - If query is direct YouTube URL or ID, use oEmbed fast path (no VideosSearch).
    - Deduplicate concurrent identical queries using _PENDING map.
    - Cache results with TTL.
    - Fall back to VideosSearch when necessary.
    """
    key = f"videos:{query}:{limit}"
    now = time.time()

    # 1) Try cache
    async with _CACHE_LOCK:
        entry = _CACHE.get(key)
        if entry and (now - entry[0]) < CACHE_TTL:
            return entry[1]

    # 2) Deduplicate: if same query is in progress, await it
    async with _PENDING_LOCK:
        pending = _PENDING.get(key)
        if pending:
            try:
                return await pending
            except Exception:
                # If the pending task failed, continue and try again
                pass

        # create task and store it
        task = asyncio.create_task(_do_search(query, limit, key))
        _PENDING[key] = task

    try:
        res = await task
        return res
    finally:
        async with _PENDING_LOCK:
            _PENDING.pop(key, None)


async def _do_search(query: str, limit: int, cache_key: str):
    """
    Actual search implementation. Uses oEmbed for URLs/IDs for speed, otherwise VideosSearch.
    """
    # Constrain concurrency for external calls
    async with _CONCURRENCY_SEMAPHORE:
        # Fast path: if query is a YouTube URL or looks like an ID -> use oEmbed
        vid = extract_video_id(query)
        if vid:
            # construct canonical url
            if "youtube" in query or "youtu.be" in query:
                url = query
            else:
                url = f"https://www.youtube.com/watch?v={vid}"
            results = await _fetch_oembed(url, vid)
            if results:
                async with _CACHE_LOCK:
                    _CACHE[cache_key] = (time.time(), results)
                return results

        # Otherwise fallback to VideosSearch (network call)
        last_exc = None
        backoff = 1.0
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                vs = VideosSearch(query, limit=limit)
                # VideosSearch.next() is awaited by py_yt; keep as-is
                res = await vs.next()
                results = res.get("result", [])
                async with _CACHE_LOCK:
                    _CACHE[cache_key] = (time.time(), results)
                return results
            except Exception as e:
                last_exc = e
                logger.warning("VideosSearch attempt %s failed for %s: %s", attempt, query, e)
                if attempt < MAX_RETRIES:
                    await asyncio.sleep(backoff)
                    backoff *= RETRY_BACKOFF
        logger.error("VideosSearch failed after %s attempts for %s: %s", MAX_RETRIES, query, last_exc)
        return []


async def _fetch_oembed(video_url: str, vid: str = None):
    """
    Use YouTube oEmbed endpoint for a very fast metadata lookup for public videos.
    Returns a list with one result in the same shape as VideosSearch result to keep compatibility.
    """
    try:
        session = await _get_session()
        params = {"url": video_url, "format": "json"}
        async with session.get("https://www.youtube.com/oembed", params=params, timeout=aiohttp.ClientTimeout(total=5)) as resp:
            if resp.status == 200:
                data = await resp.json()
                thumbnail = data.get("thumbnail_url", "")
                # oEmbed doesn't provide duration; leave duration None (caller should handle)
                result = {
                    "title": data.get("title"),
                    "duration": None,
                    "thumbnails": [{"url": thumbnail}],
                    "id": vid or extract_video_id(video_url) or "",
                    "link": video_url,
                }
                return [result]
            else:
                # If oEmbed fails (e.g., for some restricted videos), return empty so fallback happens
                return []
    except Exception as e:
        logger.debug("oEmbed fetch failed for %s: %s", video_url, e)
        return []


async def download_with_retries(session, url, dest_path, timeout_total=300):
    last_exc = None
    backoff = 1.0
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=timeout_total)) as resp:
                if resp.status != 200:
                    raise RuntimeError(f"bad response status: {resp.status}")
                # write in streaming fashion to avoid memory spikes
                with open(dest_path, "wb") as f:
                    async for chunk in resp.content.iter_chunked(16384):
                        if not chunk:
                            continue
                        f.write(chunk)
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


async def yt_dlp_fallback_download(video_url: str, download_dir: str, video_id: str, is_audio: bool, final_path: str):
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


async def download_song(link: str) -> str:
    global YOUR_API_URL
    if not YOUR_API_URL:
        await load_api_url()
        if not YOUR_API_URL:
            YOUR_API_URL = FALLBACK_API_URL

    video_id = extract_video_id(link)
    if not video_id or len(video_id) < 3:
        return None

    DOWNLOAD_DIR = "downloads"
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    file_path = os.path.join(DOWNLOAD_DIR, f"{video_id}.mp3")

    if os.path.exists(file_path):
        return file_path

    tried_api = False
    try:
        session = await _get_session()
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
    video_url = link if "youtube" in link or "youtu.be" in link else f"https://www.youtube.com/watch?v={video_id}"
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


async def download_video(link: str) -> str:
    global YOUR_API_URL
    if not YOUR_API_URL:
        await load_api_url()
        if not YOUR_API_URL:
            YOUR_API_URL = FALLBACK_API_URL

    video_id = extract_video_id(link)
    if not video_id or len(video_id) < 3:
        return None

    DOWNLOAD_DIR = "downloads"
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    file_path = os.path.join(DOWNLOAD_DIR, f"{video_id}.mp4")

    if os.path.exists(file_path):
        return file_path

    tried_api = False
    try:
        session = await _get_session()
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
    video_url = link if "youtube" in link or "youtu.be" in link else f"https://www.youtube.com/watch?v={video_id}"
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


async def shell_cmd(cmd):
    proc = await asyncio.create_subprocess_shell(
        cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, errorz = await proc.communicate()
    if errorz:
        if "unavailable videos are hidden" in (errorz.decode("utf-8")).lower():
            return out.decode("utf-8")
        else:
            return errorz.decode("utf-8")
    return out.decode("utf-8")


class YouTubeAPI:
    def __init__(self):
        self.base = "https://www.youtube.com/watch?v="
        self.regex = r"(?:youtube\\.com|youtu\\.be)"
        self.status = "https://www.youtube.com/oembed?url="
        self.listbase = "https://youtube.com/playlist?list="
        self.reg = re.compile(r"\\x1B(?:[@-Z\\\\-_]|\\[[0-?]*[ -/] *[@-~])")

    async def exists(self, link: str, videoid: Union[bool, str] = None):
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
                        text = message.text or message.caption
                        return text[entity.offset: entity.offset + entity.length]
                    if entity.type == MessageEntityType.TEXT_LINK:
                        return entity.url
            if message.caption_entities:
                for entity in message.caption_entities:
                    if entity.type == MessageEntityType.TEXT_LINK:
                        return entity.url
            text_content = (message.text or "") + " \n " + (message.caption or "")
            for m in YOUTUBE_URL_RE.finditer(text_content):
                candidate = m.group('url')
                if candidate:
                    return candidate
        return None

    async def details(self, link: str, videoid: Union[bool, str] = None):
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

    async def title(self, link: str, videoid: Union[bool, str] = None):
        if videoid:
            link = self.base + link
        if "&" in link:
            link = link.split("&")[0]
        results = await _cached_search(link, limit=1)
        if not results:
            return None
        return results[0].get("title")

    async def duration(self, link: str, videoid: Union[bool, str] = None):
        if videoid:
            link = self.base + link
        if "&" in link:
            link = link.split("&")[0]
        results = await _cached_search(link, limit=1)
        if not results:
            return None
        return results[0].get("duration")

    async def thumbnail(self, link: str, videoid: Union[bool, str] = None):
        if videoid:
            link = self.base + link
        if "&" in link:
            link = link.split("&")[0]
        results = await _cached_search(link, limit=1)
        if not results:
            return None
        return results[0].get("thumbnails", [{}])[0].get("url", "").split("?")[0]

    async def video(self, link: str, videoid: Union[bool, str] = None):
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

    async def playlist(self, link, limit, user_id, videoid: Union[bool, str] = None):
        if videoid:
            link = self.listbase + link
        if "&" in link:
            link = link.split("&")[0]
        cookiefile = get_cookiefile()
        cookie_arg = f"--cookies {cookiefile}" if cookiefile else ""
        playlist_cmd = f"yt-dlp -i --get-id --flat-playlist --playlist-end {limit} --skip-download {cookie_arg} {link}".strip()
        playlist = await shell_cmd(playlist_cmd)
        try:
            result = [key for key in playlist.split("\n") if key]
        except:
            result = []
        return result

    async def track(self, link: str, videoid: Union[bool, str] = None):
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

    async def formats(self, link: str, videoid: Union[bool, str] = None):
        if videoid:
            link = self.base + link
        if "&" in link:
            link = link.split("&")[0]
        cookiefile = get_cookiefile()
        ytdl_opts = {"quiet": True}
        if cookiefile:
            ytdl_opts["cookiefile"] = cookiefile
            logger.info("formats() will use cookie file: %s", cookiefile)
        ydl = yt_dlp.YoutubeDL(ytdl_opts)
        with ydl:
            formats_available = []
            r = ydl.extract_info(link, download=False)
            for format in r.get("formats", []):
                try:
                    if "dash" not in str(format.get("format", "")).lower():
                        formats_available.append(
                            {
                                "format": format.get("format"),
                                "filesize": format.get("filesize"),
                                "format_id": format.get("format_id"),
                                "ext": format.get("ext"),
                                "format_note": format.get("format_note"),
                                "yturl": link,
                            }
                        )
                except:
                    continue
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
    ) -> str:
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
