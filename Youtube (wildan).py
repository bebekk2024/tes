import asyncio
import os
import re
import json
from typing import Union
import yt_dlp
from pyrogram.enums import MessageEntityType
from pyrogram.types import Message
from youtubesearchpython.__future__ import VideosSearch
from ShrutiMusic import app, LOGGER
from ShrutiMusic.utils.formatters import time_to_seconds
import random
import aiohttp
from urllib.parse import urlparse

YOUR_API_URL = None

def cookie_txt_file():
    cookie_dir = "ShrutiMusic/cookies"
    if not os.path.exists(cookie_dir):
        return None
    cookies_files = [f for f in os.listdir(cookie_dir) if f.endswith(".txt")]
    if not cookies_files:
        return None
    cookie_file = os.path.join(cookie_dir, random.choice(cookies_files))
    return cookie_file

async def load_api_url():
    global YOUR_API_URL
    logger = LOGGER("ShrutiMusic/platforms/Youtube.py")
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get("https://pastebin.com/raw/rLsBhAQa") as response:
                if response.status == 200:
                    content = await response.text()
                    YOUR_API_URL = content.strip()
                    logger.info("API URL loaded successfully")
                else:
                    logger.error(f"Failed to fetch API URL. HTTP Status: {response.status}")
    except Exception as e:
        logger.error(f"Error loading API URL: {e}")

async def get_api_url():
    global YOUR_API_URL
    if not YOUR_API_URL:
        await load_api_url()
    return YOUR_API_URL

async def get_telegram_file(telegram_link: str, video_id: str, file_type: str) -> str:
    logger = LOGGER("ShrutiMusic/platforms/Youtube.py")
    try:
        extension = ".webm" if file_type == "audio" else ".mkv"
        file_path = os.path.join("downloads", f"{video_id}{extension}")

        if os.path.exists(file_path):
            logger.info(f"📂 [LOCAL] File exists: {video_id}")
            return file_path

        parsed = urlparse(telegram_link)
        parts = parsed.path.strip("/").split("/")
        if len(parts) < 2:
            logger.error(f"❌ Invalid Telegram link format: {telegram_link}")
            return None
        channel_name = parts[0]
        try:
            message_id = int(parts[1])
        except ValueError:
            logger.error(f"❌ Invalid message_id in Telegram link: {telegram_link}")
            return None

        logger.info(f"📥 [TELEGRAM] Downloading from @{channel_name}/{message_id}")
        msg = await app.get_messages(channel_name, message_id)
        os.makedirs("downloads", exist_ok=True)
        await msg.download(file_name=file_path)
        timeout = 0
        while not os.path.exists(file_path) and timeout < 60:
            await asyncio.sleep(0.5)
            timeout += 0.5
        if os.path.exists(file_path):
            logger.info(f"✅ [TELEGRAM] Downloaded: {video_id}")
            return file_path
        else:
            logger.error(f"❌ [TELEGRAM] Timeout: {video_id}")
            return None
    except Exception as e:
        logger.error(f"❌ [TELEGRAM] Failed to download {video_id}: {e}")
        return None

async def download_song(link: str) -> str:
    logger = LOGGER("ShrutiMusic/platforms/Youtube.py")
    api_url = await get_api_url()
    if not api_url:
        logger.error("API URL not available")
        return None
    video_id = link.split('v=')[-1].split('&')[0] if 'v=' in link else link
    logger.info(f"🎵 [AUDIO] Starting download for: {video_id}")

    if not video_id or len(video_id) < 3:
        return None

    DOWNLOAD_DIR = "downloads"
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    file_path = os.path.join(DOWNLOAD_DIR, f"{video_id}.webm")
    if os.path.exists(file_path):
        logger.info(f"🎵 [LOCAL] File exists: {video_id}")
        return file_path
    try:
        async with aiohttp.ClientSession() as session:
            params = {"url": video_id, "type": "audio"}
            async with session.get(
                f"{api_url}/download", params=params, timeout=aiohttp.ClientTimeout(total=60)
            ) as response:
                data = await response.json()
                if response.status != 200:
                    logger.error(f"[AUDIO] API error: {response.status}")
                    return None
                if data.get("link") and "t.me" in str(data.get("link")):
                    telegram_link = data["link"]
                    logger.info(f"🔗 [AUDIO] Telegram link received: {telegram_link}")
                    downloaded_file = await get_telegram_file(telegram_link, video_id, "audio")
                    if downloaded_file:
                        return downloaded_file
                    else:
                        logger.warning(f"⚠️ [AUDIO] Telegram download failed")
                        return None
                elif data.get("status") == "success" and data.get("stream_url"):
                    stream_url = data["stream_url"]
                    logger.info(f"[AUDIO] Stream URL obtained: {video_id}")
                    async with session.get(stream_url, timeout=aiohttp.ClientTimeout(total=300)) as file_response:
                        if file_response.status != 200:
                            logger.error(f"[AUDIO] Download failed: {file_response.status}")
                            return None
                        with open(file_path, "wb") as f:
                            async for chunk in file_response.content.iter_chunked(16384):
                                f.write(chunk)
                        logger.info(f"🎉 [AUDIO] Downloaded: {video_id}")
                        return file_path
                else:
                    logger.error(f"[AUDIO] Invalid response: {data}")
                    return None
    except asyncio.TimeoutError:
        logger.error(f"[AUDIO] Timeout: {video_id}")
        return None
    except Exception as e:
        logger.error(f"[AUDIO] Exception: {video_id} - {e}")
        return None

async def download_video(link: str) -> str:
    logger = LOGGER("ShrutiMusic/platforms/Youtube.py")
    api_url = await get_api_url()
    if not api_url:
        logger.error("API URL not available")
        return None
    video_id = link.split('v=')[-1].split('&')[0] if 'v=' in link else link
    logger.info(f"🎥 [VIDEO] Starting download for: {video_id}")
    if not video_id or len(video_id) < 3:
        return None
    DOWNLOAD_DIR = "downloads"
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    file_path = os.path.join(DOWNLOAD_DIR, f"{video_id}.mkv")
    if os.path.exists(file_path):
        logger.info(f"🎥 [LOCAL] File exists: {video_id}")
        return file_path
    try:
        async with aiohttp.ClientSession() as session:
            params = {"url": video_id, "type": "video"}
            async with session.get(f"{api_url}/download", params=params, timeout=aiohttp.ClientTimeout(total=60)) as response:
                data = await response.json()
                if response.status != 200:
                    logger.error(f"[VIDEO] API error: {response.status}")
                    return None
                if data.get("link") and "t.me" in str(data.get("link")):
                    telegram_link = data["link"]
                    logger.info(f"🔗 [VIDEO] Telegram link received: {telegram_link}")
                    downloaded_file = await get_telegram_file(telegram_link, video_id, "video")
                    if downloaded_file:
                        return downloaded_file
                    else:
                        logger.warning(f"⚠️ [VIDEO] Telegram download failed")
                        return None
                elif data.get("status") == "success" and data.get("stream_url"):
                    stream_url = data["stream_url"]
                    logger.info(f"[VIDEO] Stream URL obtained: {video_id}")
                    async with session.get(stream_url, timeout=aiohttp.ClientTimeout(total=600)) as file_response:
                        if file_response.status != 200:
                            logger.error(f"[VIDEO] Download failed: {file_response.status}")
                            return None
                        with open(file_path, "wb") as f:
                            async for chunk in file_response.content.iter_chunked(16384):
                                f.write(chunk)
                        logger.info(f"🎉 [VIDEO] Downloaded: {video_id}")
                        return file_path
                else:
                    logger.error(f"[VIDEO] Invalid response: {data}")
                    return None
    except asyncio.TimeoutError:
        logger.error(f"[VIDEO] Timeout: {video_id}")
        return None
    except Exception as e:
        logger.error(f"[VIDEO] Exception: {video_id} - {e}")
        return None

async def check_file_size(link):
    def parse_size(formats):
        total_size = 0
        for format in formats:
            if 'filesize' in format and format['filesize']:
                total_size += format['filesize']
        return total_size

    async def get_format_info(link):
        cookie_file = cookie_txt_file()
        if not cookie_file:
            print("No cookies found. Cannot check file size.")
            return None
        proc = await asyncio.create_subprocess_exec(
            "yt-dlp",
            "--cookies", cookie_file,
            "-J",
            link,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            print(f'Error:\n{stderr.decode()}')
            return None
        return json.loads(stdout.decode())
    info = await get_format_info(link)
    if info is None:
        return None
    formats = info.get('formats', [])
    if not formats:
        print("No formats found.")
        return None
    total_size = parse_size(formats)
    return total_size

async def shell_cmd(cmd):
    proc = await asyncio.create_subprocess_shell(
        cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )
    out, errorz = await proc.communicate()
    err = errorz.decode("utf-8")
    if errorz and "unavailable videos are hidden" in err.lower():
        return out.decode("utf-8")
    elif errorz:
        return err
    return out.decode("utf-8")

class YouTubeAPI:
    def __init__(self):
        self.base = "https://www.youtube.com/watch?v="
        self.regex = r"(?:youtube\.com|youtu\.be)"
        self.status = "https://www.youtube.com/oembed?url="
        self.listbase = "https://youtube.com/playlist?list="
        self.reg = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
        self.logger = LOGGER("ShrutiMusic/platforms/Youtube.py")

    async def exists(self, link: str, videoid: Union[bool, str] = None):
        if videoid:
            link = self.base + str(link)
        return bool(re.search(self.regex, link))

    async def url(self, message_1: Message) -> Union[str, None]:
        messages = [message_1]
        if getattr(message_1, 'reply_to_message', False):
            messages.append(message_1.reply_to_message)
        for message in messages:
            if getattr(message, "entities", None):
                for entity in message.entities:
                    if entity.type == MessageEntityType.URL:
                        text = getattr(message, "text", None) or getattr(message, "caption", None)
                        if text:
                            return text[entity.offset: entity.offset + entity.length]
            if getattr(message, "caption_entities", None):
                for entity in message.caption_entities:
                    if entity.type == MessageEntityType.TEXT_LINK:
                        return entity.url
        return None

    async def details(self, link: str, videoid: Union[bool, str] = None):
        if videoid:
            link = self.base + str(link)
        if "&" in link:
            link = link.split("&")[0]
        results = VideosSearch(link, limit=1)
        resultdata = (await results.next()).get("result", [])
        if not resultdata:
            return (None, None, 0, None, None)
        result = resultdata[0]
        title = result.get("title")
        duration_min = result.get("duration")
        thumbnail = result["thumbnails"][0]["url"].split("?")[0] if result.get("thumbnails") else None
        vidid = result.get("id")
        duration_sec = int(time_to_seconds(duration_min)) if duration_min else 0
        return title, duration_min, duration_sec, thumbnail, vidid

    async def title(self, link: str, videoid: Union[bool, str] = None):
        if videoid:
            link = self.base + str(link)
        if "&" in link:
            link = link.split("&")[0]
        results = VideosSearch(link, limit=1)
        resultdata = (await results.next()).get("result", [])
        return resultdata[0]["title"] if resultdata else None

    async def duration(self, link: str, videoid: Union[bool, str] = None):
        if videoid:
            link = self.base + str(link)
        if "&" in link:
            link = link.split("&")[0]
        results = VideosSearch(link, limit=1)
        resultdata = (await results.next()).get("result", [])
        return resultdata[0]["duration"] if resultdata else None

    async def thumbnail(self, link: str, videoid: Union[bool, str] = None):
        if videoid:
            link = self.base + str(link)
        if "&" in link:
            link = link.split("&")[0]
        results = VideosSearch(link, limit=1)
        resultdata = (await results.next()).get("result", [])
        return resultdata[0]["thumbnails"][0]["url"].split("?")[0] if resultdata else None

    async def video(self, link: str, videoid: Union[bool, str] = None):
        if videoid:
            link = self.base + str(link)
        if "&" in link:
            link = link.split("&")[0]
        try:
            downloaded_file = await download_video(link)
            if downloaded_file:
                return 1, downloaded_file
            else:
                return 0, "Video download failed"
        except Exception as e:
            return 0, f"Video download error: {e}"

    async def playlist(self, link, limit, user_id, videoid: Union[bool, str] = None):
        if videoid:
            link = self.listbase + str(link)
        if "&" in link:
            link = link.split("&")[0]
        cookie_file = cookie_txt_file()
        if not cookie_file:
            return []
        playlist_raw = await shell_cmd(
            f"yt-dlp -i --get-id --flat-playlist --cookies {cookie_file} --playlist-end {limit} --skip-download {link}"
        )
        return [key.strip() for key in playlist_raw.split("\n") if key.strip()]

    async def track(self, link: str, videoid: Union[bool, str] = None):
        if videoid:
            link = self.base + str(link)
        if "&" in link:
            link = link.split("&")[0]
        results = VideosSearch(link, limit=1)
        resultdata = (await results.next()).get("result", [])
        if not resultdata:
            return {}, None
        result = resultdata[0]
        track_details = {
            "title": result.get("title"),
            "link": result.get("link"),
            "vidid": result.get("id"),
            "duration_min": result.get("duration"),
            "thumb": result["thumbnails"][0]["url"].split("?")[0] if result.get("thumbnails") else None,
        }
        vidid = result.get("id")
        return track_details, vidid

    async def formats(self, link: str, videoid: Union[bool, str] = None):
        if videoid:
            link = self.base + str(link)
        if "&" in link:
            link = link.split("&")[0]
        cookie_file = cookie_txt_file()
        if not cookie_file:
            return [], link
        ytdl_opts = {"quiet": True, "cookiefile": cookie_file}
        def _extract_formats():
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
                    except Exception:
                        continue
                return formats_available, link
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, _extract_formats)

    async def slider(self, link: str, query_type: int, videoid: Union[bool, str] = None):
        if videoid:
            link = self.base + str(link)
        if "&" in link:
            link = link.split("&")[0]
        a = VideosSearch(link, limit=10)
        result = (await a.next()).get("result", [])
        if not result or query_type >= len(result):
            return None, None, None, None
        res = result[query_type]
        title = res.get("title")
        duration_min = res.get("duration")
        vidid = res.get("id")
        thumbnail = res["thumbnails"][0]["url"].split("?")[0] if res.get("thumbnails") else None
        return title, duration_min, thumbnail, vidid

    async def download(
        self,
        link: str,
        mystic=None,
        video: Union[bool, str] = None,
        videoid: Union[bool, str] = None,
        songaudio: Union[bool, str] = None,
        songvideo: Union[bool, str] = None,
        format_id: Union[bool, str] = None,
        title: Union[bool, str] = None,
    ) -> str:
        if videoid:
            link = self.base + str(link)
        try:
            if songvideo:
                downloaded_file = await download_video(link)
                if downloaded_file:
                    return downloaded_file, True
                else:
                    return None, False
            elif songaudio or not video:
                downloaded_file = await download_song(link)
                if downloaded_file:
                    return downloaded_file, True
                else:
                    return None, False
            elif video:
                downloaded_file = await download_video(link)
                if downloaded_file:
                    return downloaded_file, True
                else:
                    return None, False
            else:
                downloaded_file = await download_song(link)
                if downloaded_file:
                    return downloaded_file, True
                else:
                    return None, False
        except Exception as e:
            self.logger.error(f"Download failed: {e}")
            return None, False
