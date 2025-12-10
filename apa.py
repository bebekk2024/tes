# If you reuse this file in another repo, update module-name strings for LOGGER calls (line ~10)
import asyncio
import os
import re
import time
import glob
import shutil
from typing import Union, Optional, Any, Tuple, List
import yt_dlp
from pyrogram.enums import MessageEntityType
from pyrogram.types import Message
from py_yt import VideosSearch
from ShrutiMusic.utils.formatters import time_to_seconds
import aiohttp
from ShrutiMusic import LOGGER

# optional aiofiles for non-blocking file writes
try:
    import aiofiles  # type: ignore

    _HAVE_AIOFILES = True
except Exception:
    _HAVE_AIOFILES = False

YOUR_API_URL = None
FALLBACK_API_URL = "https://shrutibots.site"

logger = LOGGER("ShrutiMusic.platforms.Youtube.py")


async def load_api_url():
    global YOUR_API_URL
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                "https://pastebin.com/raw/rLsBhAQa", timeout=aiohttp.ClientTimeout(total=10)
            ) as response:
                if response.status == 200:
                    content = await response.text()
                    YOUR_API_URL = content.strip()
                    logger.info("API URL loaded successfully")
                else:
                    YOUR_API_URL = FALLBACK_API_URL
                    logger.info("Using fallback API URL (status %s)", response.status)
    except Exception as e:
        YOUR_API_URL = FALLBACK_API_URL
        logger.info("Using fallback API URL due to error: %s", e)


# schedule loader at import-time without blocking (safe in both sync and async contexts)
try:
    try:
        loop = asyncio.get_running_loop()
        # running loop: schedule background task
        loop.create_task(load_api_url())
    except RuntimeError:
        # no running loop: run loader in a background thread to avoid blocking import
        import threading

        def _run_loader_in_thread():
            try:
                asyncio.run(load_api_url())
            except Exception:
                pass

        t = threading.Thread(target=_run_loader_in_thread, daemon=True)
        t.start()
except Exception as e:
    logger.debug("Failed to schedule load_api_url at import-time: %s", e)


# Robust video id extraction
def extract_video_id(link: str) -> Optional[str]:
    if not link:
        return None
    link = link.strip()
    # quick check: pure 11-char id
    if re.fullmatch(r"[A-Za-z0-9_-]{11}", link):
        return link
    # common url patterns (prefer 11-char ids)
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
    # fallback: accept shorter ids (backwards compat)
    alt = re.search(r"(?:v=|youtu\.be\/)([A-Za-z0-9_-]{6,11})", link)
    if alt:
        return alt.group(1)
    # last resort: if link looks like a url and contains v=, grab it
    if "v=" in link:
        vid = link.split("v=")[-1].split("&")[0]
        if re.fullmatch(r"[A-Za-z0-9_-]{6,11}", vid):
            return vid
    return None


# helper: try to extract a URL from various API response shapes
def _extract_stream_url_from_api_response(data: Any) -> Optional[str]:
    if not data:
        return None
    if isinstance(data, dict):
        for key in ("stream_url", "url", "download_url", "audio_url", "file"):
            val = data.get(key)
            if isinstance(val, str) and val.startswith(("http://", "https://")):
                return val
        for parent in ("data", "result", "response"):
            p = data.get(parent)
            if isinstance(p, dict):
                for key in ("stream_url", "url", "download_url"):
                    val = p.get(key)
                    if isinstance(val, str) and val.startswith(("http://", "https://")):
                        return val
    if isinstance(data, str) and data.startswith(("http://", "https://")):
        return data
    return None


# safe download helper using aiofiles if present or executor fallback
async def _write_response_to_file(resp: aiohttp.ClientResponse, dest_path: str) -> bool:
    tmp = dest_path + ".part"
    os.makedirs(os.path.dirname(dest_path) or ".", exist_ok=True)
    try:
        if _HAVE_AIOFILES:
            import aiofiles  # re-import for type-checkers
            async with aiofiles.open(tmp, "wb") as f:
                async for chunk in resp.content.iter_chunked(64 * 1024):
                    if not chunk:
                        continue
                    await f.write(chunk)
        else:
            # synchronous write in executor to avoid blocking event loop
            loop = asyncio.get_event_loop()

            def _sync_write():
                with open(tmp, "wb") as f:
                    # read .content.iter_chunked synchronously is not possible, so we stream via .read()
                    # use resp.content.read() in chunks via blocking read using aiohttp's underlying transport is tricky;
                    # instead we'll fetch full body (acceptable for moderate sizes) — this is fallback only.
                    data = asyncio.run(resp.read()) if False else None
                    if data is not None:
                        f.write(data)
                    else:
                        # last-resort: attempt simple blocking iteration (may not work in all aiohttp versions)
                        pass

            # attempt to stream via chunked async loop and blocking write to keep simple:
            with open(tmp, "wb") as f:
                async for chunk in resp.content.iter_chunked(64 * 1024):
                    if not chunk:
                        continue
                    f.write(chunk)
        # atomic move
        shutil.move(tmp, dest_path)
        return True
    except Exception as e:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass
        logger.exception("Failed writing stream to file %s: %s", dest_path, e)
        return False


async def download_song(link: str) -> Optional[str]:
    global YOUR_API_URL
    if not YOUR_API_URL:
        await load_api_url()
        if not YOUR_API_URL:
            YOUR_API_URL = FALLBACK_API_URL

    video_id = extract_video_id(link) or ""
    if not video_id or len(video_id) < 3:
        return None

    DOWNLOAD_DIR = "downloads"
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    file_path = os.path.join(DOWNLOAD_DIR, f"{video_id}.mp3")
    if os.path.exists(file_path):
        return file_path

    # prefer full video url for external API
    video_url = link if ("youtube" in link or "youtu.be" in link) else f"https://www.youtube.com/watch?v={video_id}"

    try:
        async with aiohttp.ClientSession() as session:
            params = {"url": video_url, "type": "audio"}
            last_exc = None
            for attempt in range(1, 3):
                try:
                    async with session.get(f"{YOUR_API_URL}/download", params=params, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                        text = await resp.text()
                        if resp.status != 200:
                            logger.warning("downloader api returned %s for %s: %s", resp.status, video_url, text[:300])
                            raise RuntimeError(f"downloader api returned {resp.status}")
                        try:
                            data = await resp.json()
                        except Exception:
                            data = text
                        logger.debug("downloader API response for %s: %s", video_url, data)
                        stream_url = _extract_stream_url_from_api_response(data)
                        if not stream_url:
                            raise RuntimeError("no stream_url in api response")
                        async with session.get(stream_url, timeout=aiohttp.ClientTimeout(total=300)) as file_resp:
                            if file_resp.status != 200:
                                raise RuntimeError(f"stream responded {file_resp.status}")
                            ok = await _write_response_to_file(file_resp, file_path)
                            if ok:
                                return file_path
                            else:
                                raise RuntimeError("failed write stream to disk")
                except Exception as e:
                    last_exc = e
                    logger.warning("download_song api attempt %s failed for %s: %s", attempt, video_url, e)
                    await asyncio.sleep(1.5)
            logger.error("download_song API method failed for %s after retries: %s", video_url, last_exc)
    except Exception as e:
        logger.exception("download_song unexpected error while using API: %s", e)

    # fallback to yt_dlp (run in executor since yt_dlp is blocking)
    logger.info("Falling back to yt_dlp for audio: %s", video_url)

    def _yt_download_audio_sync(video_url_inner: str, outdir: str, vidid: str):
        try:
            ydl_opts = {
                "format": "bestaudio/best",
                "outtmpl": os.path.join(outdir, f"{vidid}.%(ext)s"),
                "quiet": True,
                "noplaylist": True,
                "restrictfilenames": True,
            }
            cookie = None
            cookie_env = os.environ.get("YTDL_COOKIE_FILE")
            if cookie_env and os.path.isfile(cookie_env):
                cookie = cookie_env
            elif os.path.isfile("cookies.txt"):
                cookie = "cookies.txt"
            if cookie:
                ydl_opts["cookiefile"] = cookie
                logger.info("yt_dlp will use cookie file: %s", cookie)
            ydl_opts["postprocessors"] = [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}]
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([video_url_inner])
            # select produced file
            patterns = glob.glob(os.path.join(outdir, f"{vidid}.*"))
            for ext in (".mp3", ".m4a", ".webm", ".opus", ".aac"):
                for p in patterns:
                    if p.lower().endswith(ext):
                        return p
            return patterns[0] if patterns else None
        except Exception as e:
            logger.exception("yt_dlp audio download error: %s", e)
            return None

    loop = asyncio.get_event_loop()
    produced = await loop.run_in_executor(None, _yt_download_audio_sync, video_url, DOWNLOAD_DIR, video_id)
    if produced:
        try:
            if os.path.abspath(produced) != os.path.abspath(file_path):
                if os.path.exists(file_path):
                    os.remove(file_path)
                shutil.move(produced, file_path)
            return file_path
        except Exception:
            logger.exception("failed moving yt_dlp produced file")
            return produced
    return None


async def download_video(link: str) -> Optional[str]:
    global YOUR_API_URL
    if not YOUR_API_URL:
        await load_api_url()
        if not YOUR_API_URL:
            YOUR_API_URL = FALLBACK_API_URL

    video_id = extract_video_id(link) or ""
    if not video_id or len(video_id) < 3:
        return None

    DOWNLOAD_DIR = "downloads"
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    file_path = os.path.join(DOWNLOAD_DIR, f"{video_id}.mp4")
    if os.path.exists(file_path):
        return file_path

    video_url = link if ("youtube" in link or "youtu.be" in link) else f"https://www.youtube.com/watch?v={video_id}"

    try:
        async with aiohttp.ClientSession() as session:
            params = {"url": video_url, "type": "video"}
            last_exc = None
            for attempt in range(1, 3):
                try:
                    async with session.get(f"{YOUR_API_URL}/download", params=params, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                        text = await resp.text()
                        if resp.status != 200:
                            logger.warning("downloader api returned %s for %s: %s", resp.status, video_url, text[:300])
                            raise RuntimeError(f"downloader api returned {resp.status}")
                        try:
                            data = await resp.json()
                        except Exception:
                            data = text
                        logger.debug("downloader API response for %s: %s", video_url, data)
                        stream_url = _extract_stream_url_from_api_response(data)
                        if not stream_url:
                            raise RuntimeError("no stream_url in api response")
                        async with session.get(stream_url, timeout=aiohttp.ClientTimeout(total=600)) as file_resp:
                            if file_resp.status != 200:
                                raise RuntimeError(f"stream responded {file_resp.status}")
                            ok = await _write_response_to_file(file_resp, file_path)
                            if ok:
                                return file_path
                            else:
                                raise RuntimeError("failed write stream to disk")
                except Exception as e:
                    last_exc = e
                    logger.warning("download_video api attempt %s failed for %s: %s", attempt, video_url, e)
                    await asyncio.sleep(1.5)
            logger.error("download_video API method failed for %s after retries: %s", video_url, last_exc)
    except Exception as e:
        logger.exception("download_video unexpected error while using API: %s", e)

    # fallback to yt_dlp
    logger.info("Falling back to yt_dlp for video: %s", video_url)

    def _yt_download_video_sync(video_url_inner: str, outdir: str, vidid: str):
        try:
            ydl_opts = {
                "format": "bestvideo+bestaudio/best",
                "outtmpl": os.path.join(outdir, f"{vidid}.%(ext)s"),
                "quiet": True,
                "noplaylist": True,
                "restrictfilenames": True,
            }
            cookie = None
            cookie_env = os.environ.get("YTDL_COOKIE_FILE")
            if cookie_env and os.path.isfile(cookie_env):
                cookie = cookie_env
            elif os.path.isfile("cookies.txt"):
                cookie = "cookies.txt"
            if cookie:
                ydl_opts["cookiefile"] = cookie
                logger.info("yt_dlp will use cookie file: %s", cookie)
            ydl_opts["merge_output_format"] = "mp4"
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([video_url_inner])
            patterns = glob.glob(os.path.join(outdir, f"{vidid}.*"))
            for ext in (".mp4", ".mkv", ".webm"):
                for p in patterns:
                    if p.lower().endswith(ext):
                        return p
            return patterns[0] if patterns else None
        except Exception as e:
            logger.exception("yt_dlp video download error: %s", e)
            return None

    loop = asyncio.get_event_loop()
    produced = await loop.run_in_executor(None, _yt_download_video_sync, video_url, DOWNLOAD_DIR, video_id)
    if produced:
        try:
            if os.path.abspath(produced) != os.path.abspath(file_path):
                if os.path.exists(file_path):
                    os.remove(file_path)
                shutil.move(produced, file_path)
            return file_path
        except Exception:
            logger.exception("failed moving yt_dlp produced file")
            return produced
    return None


async def shell_cmd(cmd: str) -> str:
    proc = await asyncio.create_subprocess_shell(
        cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    out, err = await proc.communicate()
    out_dec = out.decode("utf-8", errors="ignore") if out else ""
    err_dec = err.decode("utf-8", errors="ignore") if err else ""
    if err_dec:
        if "unavailable videos are hidden" in err_dec.lower():
            return out_dec
        return out_dec + "\n[stderr]\n" + err_dec
    return out_dec


class YouTubeAPI:
    def __init__(self):
        self.base = "https://www.youtube.com/watch?v="
        self.regex = r"(?:youtube\.com|youtu\.be)"
        self.status = "https://www.youtube.com/oembed?url="
        self.listbase = "https://youtube.com/playlist?list="
        self.reg = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")

    async def exists(self, link: str, videoid: Union[bool, str] = None) -> bool:
        if videoid:
            link = self.base + link
        return bool(re.search(self.regex, link))

    async def url(self, message_1: Message) -> Optional[str]:
        messages = [message_1]
        if getattr(message_1, "reply_to_message", None):
            messages.append(message_1.reply_to_message)
        for message in messages:
            if getattr(message, "entities", None):
                for entity in message.entities:
                    if entity.type == MessageEntityType.URL:
                        text = message.text or message.caption or ""
                        return text[entity.offset : entity.offset + entity.length]
                    if entity.type == MessageEntityType.TEXT_LINK:
                        return entity.url
            if getattr(message, "caption_entities", None):
                for entity in message.caption_entities:
                    if entity.type == MessageEntityType.TEXT_LINK:
                        return entity.url
            text_content = (message.text or "") + " \n " + (message.caption or "")
            m = re.search(r"(?P<url>https?://[^\s]+)", text_content)
            if m:
                return m.group("url")
        return None

    async def details(self, link: str, videoid: Union[bool, str] = None) -> Tuple[Optional[str], Optional[str], int, Optional[str], Optional[str]]:
        if videoid:
            link = self.base + link
        if "&" in link:
            link = link.split("&")[0]
        vs = VideosSearch(link, limit=1)
        res = await vs.next()
        results = res.get("result", []) if isinstance(res, dict) else []
        if not results:
            return None, None, 0, None, None
        r = results[0]
        title = r.get("title")
        duration_min = r.get("duration")
        thumbnail = (r.get("thumbnails") or [{"url": None}])[0].get("url", "").split("?")[0]
        vidid = r.get("id")
        duration_sec = int(time_to_seconds(duration_min)) if duration_min else 0
        return title, duration_min, duration_sec, thumbnail, vidid

    async def title(self, link: str, videoid: Union[bool, str] = None) -> Optional[str]:
        if videoid:
            link = self.base + link
        if "&" in link:
            link = link.split("&")[0]
        vs = VideosSearch(link, limit=1)
        res = await vs.next()
        results = res.get("result", []) if isinstance(res, dict) else []
        if not results:
            return None
        return results[0].get("title")

    async def duration(self, link: str, videoid: Union[bool, str] = None) -> Optional[str]:
        if videoid:
            link = self.base + link
        if "&" in link:
            link = link.split("&")[0]
        vs = VideosSearch(link, limit=1)
        res = await vs.next()
        results = res.get("result", []) if isinstance(res, dict) else []
        if not results:
            return None
        return results[0].get("duration")

    async def thumbnail(self, link: str, videoid: Union[bool, str] = None) -> Optional[str]:
        if videoid:
            link = self.base + link
        if "&" in link:
            link = link.split("&")[0]
        vs = VideosSearch(link, limit=1)
        res = await vs.next()
        results = res.get("result", []) if isinstance(res, dict) else []
        if not results:
            return None
        return (results[0].get("thumbnails") or [{"url": None}])[0].get("url", "").split("?")[0]

    async def video(self, link: str, videoid: Union[bool, str] = None) -> Tuple[int, Optional[str]]:
        if videoid:
            link = self.base + link
        if "&" in link:
            link = link.split("&")[0]
        try:
            f = await download_video(link)
            if f:
                return 1, f
            return 0, "Video download failed"
        except Exception as e:
            logger.exception("video download error: %s", e)
            return 0, f"Video download error: {e}"

    async def playlist(self, link: str, limit: int, user_id: int, videoid: Union[bool, str] = None) -> List[str]:
        if videoid:
            link = self.listbase + link
        if "&" in link:
            link = link.split("&")[0]

        cookiefile = None
        cookie_env = os.environ.get("YTDL_COOKIE_FILE")
        if cookie_env and os.path.isfile(cookie_env):
            cookiefile = cookie_env
        elif os.path.isfile("cookies.txt"):
            cookiefile = "cookies.txt"

        def _extract(url: str, limit_inner: int, cookie_inner: Optional[str]) -> List[str]:
            opts = {"quiet": True, "skip_download": True, "extract_flat": True}
            if cookie_inner:
                opts["cookiefile"] = cookie_inner
            try:
                with yt_dlp.YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(url, download=False)
                    entries = []
                    if isinstance(info, dict) and info.get("entries"):
                        for e in info.get("entries", []):
                            if not e:
                                continue
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
        ids = await loop.run_in_executor(None, _extract, link, limit, cookiefile)
        return ids

    async def track(self, link: str, videoid: Union[bool, str] = None) -> Tuple[dict, Optional[str]]:
        if videoid:
            link = self.base + link
        if "&" in link:
            link = link.split("&")[0]
        vs = VideosSearch(link, limit=1)
        res = await vs.next()
        results = res.get("result", []) if isinstance(res, dict) else []
        if not results:
            return {}, None
        r = results[0]
        title = r.get("title")
        duration_min = r.get("duration")
        vidid = r.get("id")
        yturl = r.get("link")
        thumbnail = (r.get("thumbnails") or [{"url": None}])[0].get("url", "").split("?")[0]
        return {"title": title, "link": yturl, "vidid": vidid, "duration_min": duration_min, "thumb": thumbnail}, vidid

    async def formats(self, link: str, videoid: Union[bool, str] = None) -> Tuple[List[dict], str]:
        if videoid:
            link = self.base + link
        if "&" in link:
            link = link.split("&")[0]

        cookiefile = None
        cookie_env = os.environ.get("YTDL_COOKIE_FILE")
        if cookie_env and os.path.isfile(cookie_env):
            cookiefile = cookie_env
        elif os.path.isfile("cookies.txt"):
            cookiefile = "cookies.txt"

        def _extract_sync(url: str, cookie_inner: Optional[str]) -> Any:
            opts = {"quiet": True}
            if cookie_inner:
                opts["cookiefile"] = cookie_inner
            try:
                with yt_dlp.YoutubeDL(opts) as ydl:
                    return ydl.extract_info(url, download=False)
            except Exception as e:
                logger.exception("formats() extract_info failed in executor: %s", e)
                return None

        loop = asyncio.get_event_loop()
        info = await loop.run_in_executor(None, _extract_sync, link, cookiefile)
        formats_available: List[dict] = []
        try:
            if isinstance(info, dict) and info.get("entries"):
                entries = [e for e in info.get("entries") if e]
                if entries:
                    info = entries[0]
            for fmt in (info.get("formats", []) if info else []):
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
        vs = VideosSearch(link, limit=10)
        res = await vs.next()
        results = res.get("result", []) if isinstance(res, dict) else []
        if not results or query_type >= len(results):
            return None, None, None, None
        r = results[query_type]
        title = r.get("title")
        duration_min = r.get("duration")
        vidid = r.get("id")
        thumbnail = (r.get("thumbnails") or [{"url": None}])[0].get("url", "").split("?")[0]
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
