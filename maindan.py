# Copyright (c) 2025 Nand Yaduwanshi <NoxxOP>
# Location: Supaul, Bihar
#
# All rights reserved.
#
# This code is the intellectual property of Nand Yaduwanshi.
# You are not allowed to copy, modify, redistribute, or use this
# code for commercial or personal projects without explicit permission.
#
# Allowed:
# - Forking for personal learning
# - Submitting improvements via pull requests
#
# Not Allowed:
# - Claiming this code as your own
# - Re-uploading without credit or permission
# - Selling or using commercially
#
# Contact for permissions:
# Email: badboy809075@gmail.com

import asyncio
import importlib
import inspect
import re
import gc
import sqlite3
import signal
from pyrogram import idle
from pyrogram.types import BotCommand
from pyrogram.errors import FloodWait
from pytgcalls.exceptions import NoActiveGroupCall, NoVideoSourceFound
from pytgcalls.ffprobe import FFprobe
import config
from ShrutiMusic import LOGGER, app, userbot
from ShrutiMusic.core.call import Nand as _NAND_SYMBOL
from ShrutiMusic.misc import sudo
from ShrutiMusic.plugins import ALL_MODULES
from ShrutiMusic.utils.database import get_banned_users, get_gbanned
from config import BANNED_USERS

# Bot Commands List
COMMANDS = [
    BotCommand("start", "🚀 Start bot"),
    BotCommand("protect", "🛡️ Anti-GCast protection"),
    BotCommand("antigcast", "🛡️ Alias protect"),
    BotCommand("protectmode", "🛡️ Strict delete mode"),
    BotCommand("antigcstmode", "🛡️ Alias protectmode"),
    BotCommand("antigcstconfig", "⚙️ Configure antigcst"),
    BotCommand("free", "✅ Whitelist user"),
    BotCommand("unfree", "❌ Remove whitelist"),
    BotCommand("listwhite", "👤 List whitelist"),
    BotCommand("clearwhite", "🗑️ Clear whitelist"),
    BotCommand("addblack", "🚫 Blacklist user"),
    BotCommand("delblack", "🔓 Remove blacklist"),
    BotCommand("listblack", "📋 List blacklist"),
    BotCommand("clearblack", "🗑️ Clear blacklist"),
    BotCommand("bl", "⛔ Add text blacklist"),
    BotCommand("unbl", "✅ Remove text blacklist"),
    BotCommand("listbl", "📋 List text blacklist"),
    BotCommand("help", "❓ Help menu and Many More Management Commands"),
    BotCommand("ping", "📡 Ping and system stats"),
    BotCommand("play", "🎵 Start streaming the requested track"),
    BotCommand("vplay", "📹 Start video streaming"),
    BotCommand("playrtmps", "📺 Play Live Video"),
    BotCommand("playforce", "⚠️ Force play audio track"),
    BotCommand("vplayforce", "⚠️ Force play video track"),
    BotCommand("pause", "⏸ Pause the stream"),
    BotCommand("resume", "▶️ Resume the stream"),
    BotCommand("skip", "⏭ Skip the current track"),
    BotCommand("end", "🛑 End the stream"),
    BotCommand("stop", "🛑 Stop the stream"),
    BotCommand("queue", "📄 Show track queue"),
    BotCommand("auth", "➕ Add a user to auth list"),
    BotCommand("unauth", "➖ Remove a user from auth list"),
    BotCommand("authusers", "👥 Show list of auth users"),
    BotCommand("cplay", "📻 Channel audio play"),
    BotCommand("cvplay", "📺 Channel video play"),
    BotCommand("cplayforce", "🚨 Channel force audio play"),
    BotCommand("cvplayforce", "🚨 Channel force video play"),
    BotCommand("channelplay", "🔗 Connect group to channel"),
    BotCommand("loop", "🔁 Enable/disable loop"),
    BotCommand("stats", "📊 Bot stats"),
    BotCommand("shuffle", "🔀 Shuffle the queue"),
    BotCommand("seek", "⏩ Seek forward"),
    BotCommand("seekback", "⏪ Seek backward"),
    BotCommand("song", "🎶 Download song (mp3/mp4)"),
    BotCommand("speed", "⏩ Adjust audio playback speed (group)"),
    BotCommand("cspeed", "⏩ Adjust audio speed (channel)"),
    BotCommand("tagall", "📢 Tag everyone"),
]


def _instantiate_obj(obj, *args, **kwargs):
    """
    Safe instantiate helper:
    - If obj is None -> return None
    - If obj is a class/type -> instantiate obj(*args, **kwargs)
    - If obj is callable -> try calling obj(*args, **kwargs), fallback to returning obj on TypeError
    - Else -> return obj (assume instance)
    """
    if obj is None:
        return None
    if inspect.isclass(obj):
        try:
            return obj(*args, **kwargs)
        except Exception:
            LOGGER.exception("Failed to instantiate class %s", getattr(obj, "__name__", str(obj)))
            return None
    if callable(obj):
        try:
            return obj(*args, **kwargs)
        except TypeError:
            # callable but not intended to be called without args; assume it's already an instance-like callable
            return obj
        except Exception:
            LOGGER.exception("Exception while calling callable %s", getattr(obj, "__name__", str(obj)))
            return None
    return obj


async def setup_bot_commands():
    """Setup bot commands during startup"""
    try:
        if app is None:
            LOGGER.error("Tried to set bot commands but `app` is not initialized.")
            return
        # Set bot commands
        await app.set_bot_commands(COMMANDS)
        LOGGER.info("Bot commands set successfully!")
    except Exception as e:
        LOGGER.error("Failed to set bot commands: %s", str(e))


def _seconds_from_flood(exc: Exception) -> int | None:
    """
    Try to extract seconds from a FloodWait exception.
    Pyrogram's FloodWait may expose attributes or only the message contains the number.
    """
    # Try known attributes
    for attr in ("x", "seconds", "wait"):
        val = getattr(exc, attr, None)
        try:
            if isinstance(val, (int, float)):
                return int(val)
            if val is not None:
                return int(val)
        except Exception:
            pass
    # Fallback: regex extract first number from message
    m = re.search(r"(\d+)", str(exc))
    if m:
        try:
            return int(m.group(1))
        except Exception:
            return None
    return None


async def _start_app_with_retries(max_retries: int = 5):
    """
    Start the pyrogram app with controlled retries on FloodWait and other transient errors.
    Uses the wait time provided by Telegram when possible.
    """
    if app is None:
        LOGGER.error("app is not initialized; cannot start pyrogram client.")
        return False

    for attempt in range(1, max_retries + 1):
        try:
            LOGGER.info("Starting pyrogram client (attempt %d/%d)...", attempt, max_retries)
            await app.start()
            LOGGER.info("Pyrogram client started successfully.")
            return True
        except FloodWait as fw:
            secs = _seconds_from_flood(fw) or 5
            LOGGER.warning(
                "FloodWait received; must wait %s seconds before retrying (attempt %d/%d).",
                secs,
                attempt,
                max_retries,
            )
            # Add small margin
            await asyncio.sleep(secs + 1)
        except Exception as e:
            # Unexpected error: log and backoff exponentially
            LOGGER.exception(
                "Unexpected error while starting pyrogram client (attempt %d/%d): %s", attempt, max_retries, e
            )
            backoff = min(60, 2 ** attempt)
            await asyncio.sleep(backoff)
    LOGGER.error("Max retries reached while starting pyrogram client. Aborting start.")
    return False


# Asyncio exception handler to catch "Cannot operate on a closed database."
def _make_loop_exception_handler(loop):
    async def _restart_app_safe():
        """
        Attempt a controlled restart of app to recover from sqlite storage errors.
        
        This function handles the case where the pyrogram client gets into an
        inconsistent state due to database errors. It performs a safe restart
        with proper cleanup and retry logic to avoid ConnectionError exceptions.
        """
        try:
            if app is not None:
                LOGGER.info("Attempting to restart pyrogram app to recover from closed DB...")
                
                # Step 1: Attempt to stop the app gracefully
                try:
                    await app.stop()
                    LOGGER.info("App stopped successfully.")
                except ConnectionError as ce:
                    # If already disconnected, that's OK - continue
                    LOGGER.warning("App already disconnected: %s (this is expected)", ce)
                except Exception as e:
                    LOGGER.warning("Error stopping app during restart attempt: %s (will continue)", e)
                
                # Step 2: Wait longer for complete resource cleanup
                # This is critical to avoid "Can't disconnect an initialized client" error
                cleanup_time = 10
                LOGGER.info("Waiting %d seconds for complete resource cleanup...", cleanup_time)
                await asyncio.sleep(cleanup_time)
                
                # Step 3: Check if app is still connected (if property exists)
                if hasattr(app, 'is_connected'):
                    retry_count = 0
                    max_check_retries = 5
                    while getattr(app, 'is_connected', False) and retry_count < max_check_retries:
                        LOGGER.warning(
                            "App still connected, waiting additional second... (check %d/%d)",
                            retry_count + 1,
                            max_check_retries
                        )
                        await asyncio.sleep(1)
                        retry_count += 1
                
                # Step 4: Attempt to start with retry logic on ConnectionError
                max_start_attempts = 3
                for start_attempt in range(1, max_start_attempts + 1):
                    try:
                        LOGGER.info("Starting pyrogram app (attempt %d/%d)...", start_attempt, max_start_attempts)
                        await app.start()
                        LOGGER.info("Pyrogram app restarted successfully after closed DB recovery.")
                        return  # Success
                    except ConnectionError as ce:
                        if start_attempt < max_start_attempts:
                            wait_time = 5 * start_attempt  # 5, 10, 15 seconds
                            LOGGER.warning(
                                "ConnectionError on restart (attempt %d/%d): %s | Waiting %d seconds before retry...",
                                start_attempt,
                                max_start_attempts,
                                ce,
                                wait_time
                            )
                            await asyncio.sleep(wait_time)
                        else:
                            LOGGER.error(
                                "Failed to restart app after %d attempts. Last error: %s",
                                max_start_attempts,
                                ce
                            )
                    except Exception as ex:
                        LOGGER.exception(
                            "Unexpected error during restart (attempt %d/%d): %s",
                            start_attempt,
                            max_start_attempts,
                            ex
                        )
                        if start_attempt < max_start_attempts:
                            backoff = min(15, 2 ** start_attempt)  # Exponential backoff: 2, 4, 8, 15
                            LOGGER.info("Exponential backoff: waiting %d seconds...", backoff)
                            await asyncio.sleep(backoff)
                        
        except Exception:
            LOGGER.exception("Unexpected error during app restart attempt.")

    def handler(loop, context):
        exc = context.get("exception")
        msg = context.get("message")
        # If sqlite ProgrammingError about closed DB -> schedule restart attempt
        if isinstance(exc, sqlite3.ProgrammingError) and "closed" in str(exc).lower():
            LOGGER.exception("Detected sqlite ProgrammingError (closed DB) in async task: %s", exc)
            try:
                asyncio.create_task(_restart_app_safe())
            except Exception:
                LOGGER.exception("Failed to schedule app restart task.")
        else:
            # default logging for other unhandled exceptions
            LOGGER.error("Unhandled exception in event loop: %s | exception=%s", msg, exc)

    return handler


async def init():
    # install loop exception handler early
    loop = asyncio.get_event_loop()
    loop.set_exception_handler(_make_loop_exception_handler(loop))

    if (
        not config.STRING1
        and not config.STRING2
        and not config.STRING3
        and not config.STRING4
        and not config.STRING5
    ):
        LOGGER.error("Assistant client variables not defined, exiting...")
        exit(1)

    await sudo()

    try:
        users = await get_gbanned()
        for user_id in users:
            BANNED_USERS.add(user_id)
        users = await get_banned_users()
        for user_id in users:
            BANNED_USERS.add(user_id)
    except Exception:
        # keep going even if DB check fails
        LOGGER.exception("Failed to fetch banned user lists; continuing startup.")

    # Ensure app exists before starting
    if app is None:
        LOGGER.error("`app` is not available. Check ShrutiMusic.__init__ initialization.")
        exit(1)

    # Start the bot with controlled retries to properly handle FloodWait
    started = await _start_app_with_retries(max_retries=5)
    if not started:
        # fatal: do not continue startup to avoid repeated auth attempts
        exit(1)

    # Setup bot commands during startup
    await setup_bot_commands()

    # Import all plugin modules
    for all_module in ALL_MODULES:
        module_name = all_module.lstrip(".")
        full_name = f"ShrutiMusic.plugins.{module_name}"
        try:
            importlib.import_module(full_name)
        except Exception:
            LOGGER.exception("Failed importing plugin module %s", full_name)

    # help GC a bit after heavy imports
    try:
        gc.collect()
    except Exception:
        pass

    LOGGER.info("Successfully Imported Modules...")

    # Ensure userbot is available (try to instantiate if not provided)
    global userbot
    if userbot is None:
        try:
            from ShrutiMusic.core.userbot import Userbot as _UserbotSymbol

            userbot = _instantiate_obj(_UserbotSymbol)
            if userbot is None:
                LOGGER.error("Failed to instantiate userbot; continuing without it.")
        except Exception:
            LOGGER.exception("Failed to import/instantiate userbot; continuing without it.")

    # Prepare Nand (call client) instance from core.call
    nand_client = _instantiate_obj(_NAND_SYMBOL)
    if nand_client is None:
        LOGGER.error("Nand client is not available. Skipping call-client startup.")
    else:
        # Start userbot and nand client only if present
        if userbot is not None:
            try:
                if hasattr(userbot, "start"):
                    await userbot.start()
                    LOGGER.info("Userbot started.")
            except Exception:
                LOGGER.exception("Failed to start userbot")

        # Start Nand client
        try:
            if hasattr(nand_client, "start"):
                await nand_client.start()
                LOGGER.info("Nand client started.")
        except Exception:
            LOGGER.exception("Failed to start Nand client")

        # Try streaming to warm up call client if available
        try:
            if hasattr(nand_client, "stream_call"):
                # Allow disabling warmup via config.WARMUP = False (useful to avoid OOM during start)
                warmup_enabled = getattr(config, "WARMUP", True)
                warmup_url = getattr(config, "WARMUP_URL", "https://te.legra.ph/file/29f784eb49d230ab62e9e.mp4")
                if warmup_enabled and warmup_url:
                    try:
                        # Check file with FFprobe first to avoid unnecessary stream attempts
                        try:
                            await FFprobe.check_file(warmup_url)
                            try:
                                await nand_client.stream_call(warmup_url)
                                LOGGER.info("Warmup stream_call succeeded.")
                            except NoVideoSourceFound:
                                LOGGER.warning("Warmup: No video source found; skipping warmup stream.")
                            except Exception:
                                LOGGER.exception("Warmup stream_call failed (ignored).")
                        except NoVideoSourceFound:
                            LOGGER.warning("Warmup FFprobe: No video source found; skipping warmup.")
                        except Exception:
                            LOGGER.exception("Warmup FFprobe check failed; attempting stream_call once.")
                            try:
                                await nand_client.stream_call(warmup_url)
                            except NoVideoSourceFound:
                                LOGGER.warning("Warmup: No video source found (stream).")
                            except Exception:
                                LOGGER.exception("Warmup stream_call failed after FFprobe error (ignored).")
                    except Exception:
                        LOGGER.exception("Error during warmup stream logic (ignored).")
                else:
                    LOGGER.info("Warmup disabled by config or warmup_url empty; skipping.")
        except NoActiveGroupCall:
            LOGGER.error("Please turn on the videochat of your log group/channel.\n\nStopping Bot...")
            exit(1)
        except Exception:
            # ignore streaming errors (handled elsewhere)
            LOGGER.exception("Error while attempting stream_call (ignored)")

        try:
            if hasattr(nand_client, "decorators"):
                await nand_client.decorators()
        except Exception:
            LOGGER.exception("Error while running Nand.decorators() (ignored)")

    LOGGER.info(
        "Shruti Music Started Successfully.\n\nDon't forget to visit @Capricorn_MusicBot"
    )

    await idle()

    # graceful shutdown
    try:
        await app.stop()
    except Exception:
        LOGGER.exception("Error stopping app")

    if userbot is not None:
        try:
            await userbot.stop()
        except Exception:
            LOGGER.exception("Error stopping userbot")

    if nand_client is not None:
        try:
            if hasattr(nand_client, "stop"):
                await nand_client.stop()
        except Exception:
            LOGGER.exception("Error stopping nand client")

    LOGGER.info("Stopping Shruti Music Bot...🥺")


if __name__ == "__main__":
    # Ensure SIGTERM and SIGINT are handled by asyncio event loop on Heroku
    try:
        loop = asyncio.get_event_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, lambda s=sig: LOGGER.info("Received signal %s", s))
            except NotImplementedError:
                # Windows or unsupported platform, skip
                pass
        loop.run_until_complete(init())
    except Exception:
        LOGGER.exception("Fatal error in main entrypoint")


# ©️ Copyright Reserved - @NoxxOP  Nand Yaduwanshi

# ===========================================
# ©️ 2025 Nand Yaduwanshi (aka @NoxxOP)
# 🔗 GitHub : https://github.com/NoxxOP/ShrutiMusic
# 📢 Telegram Channel : https://t.me/ShrutiBots
# ===========================================


# ❤️ Love From ShrutiBots
