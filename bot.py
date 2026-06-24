import asyncio
import os
import uuid
import logging
import time
import mimetypes
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from aiogram import Bot, Dispatcher, types, F
from aiogram.types import (
    InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton, FSInputFile,
)
from aiogram.utils.media_group import MediaGroupBuilder
from yt_dlp import YoutubeDL
import httpx

TOKEN   = "BOT_TOKEN"
TMP_DIR = Path("./tmp")
MAX_SIZE = 50 * 1024 * 1024
MAX_CONC = 5
URL_TTL  = 1800

TMP_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(), logging.FileHandler("bot.log", encoding="utf-8")]
)
log = logging.getLogger("bot")

bot = Bot(token=TOKEN)
dp  = Dispatcher()

semaphore  = asyncio.Semaphore(MAX_CONC)
url_store: dict[int, dict] = {}
executor = ThreadPoolExecutor(max_workers=MAX_CONC)

def main_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[
            KeyboardButton(text="📥 Video"),
            KeyboardButton(text="🎵 Audio"),
        ], [
            KeyboardButton(text="🖼 Rasm"),
            KeyboardButton(text="ℹ️ Yordam"),
        ]],
        resize_keyboard=True,
        persistent=True,
    )

def _cleanup(*paths):
    for p in paths:
        try:
            if p and Path(p).exists():
                Path(p).unlink()
        except Exception as e:
            log.warning("O'chirishda xato: %s", e)

def _cleanup_list(paths: list):
    for p in paths:
        _cleanup(p)

async def _safe_delete(msg: types.Message):
    try: await msg.delete()
    except Exception: pass

async def _safe_edit(msg: types.Message, text: str):
    try: await msg.edit_text(text)
    except Exception: pass

def download_media(url: str, user_id: int, mode: str) -> str:
    uid      = uuid.uuid4().hex[:8]
    out_tmpl = str(TMP_DIR / f"{mode}_{user_id}_{uid}.%(ext)s")

    base_opts = {
        "quiet":              True,
        "no_warnings":        True,
        "nocheckcertificate": True,
        "noplaylist":         True,
        "outtmpl":            out_tmpl,
        "socket_timeout":     30,
        "retries":            3,
        "fragment_retries":   3,
        "geo_bypass":         True,
        "concurrent_fragment_downloads": 4,
        "user_agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
    }

    if mode == "v":
        base_opts["format"] = (
            "best[height<=720][ext=mp4]"
            "/best[height<=480][ext=mp4]"
            "/best[ext=mp4]"
            "/best"
        )
    else:
        base_opts["format"] = (
            "bestaudio[ext=m4a]"
            "/bestaudio[ext=mp3]"
            "/bestaudio"
            "/best"
        )

    log.info("Yuklash | user=%s mode=%s url=%s", user_id, mode, url[:60])
    t0 = time.time()

    with YoutubeDL(base_opts) as ydl:
        ydl.download([url])

    exts = ("mp4", "webm", "mkv") if mode == "v" else ("m4a", "mp3", "ogg", "opus", "webm")
    for ext in exts:
        p = TMP_DIR / f"{mode}_{user_id}_{uid}.{ext}"
        if p.exists() and p.stat().st_size > 0:
            log.info("Tayyor: %s | %.1f MB | %.1fs", p.name,
                     p.stat().st_size / 1024 / 1024, time.time() - t0)
            return str(p)

    raise FileNotFoundError("Fayl topilmadi")


def extract_images(url: str, user_id: int) -> list[str]:
    uid = uuid.uuid4().hex[:8]

    opts = {
        "quiet":              True,
        "no_warnings":        True,
        "nocheckcertificate": True,
        "noplaylist":         False,
        "socket_timeout":     30,
        "retries":            3,
        "geo_bypass":         True,
        "user_agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
    }

    log.info("Rasm yuklash | user=%s url=%s", user_id, url[:60])
    t0 = time.time()

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Referer": url,
        "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
    }

    saved: list[str] = []

    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)

        if not info:
            raise ValueError("Ma'lumot olib bo'lmadi")

        entries = list(info.get("entries") or [info])[:10]

        # URL olgan zahoti DARHOL yuklaymiz — kechiktirmaymiz
        with httpx.Client(timeout=30, follow_redirects=True) as client:
            for entry_idx, entry in enumerate(entries):

                # Thumbnail URL ni olish (None-safe)
                thumbs = entry.get("thumbnails") or []
                if thumbs:
                    best = max(
                        thumbs,
                        key=lambda t: (t.get("width") or 0) * (t.get("height") or 0)
                    )
                    img_url = best.get("url")
                else:
                    img_url = entry.get("thumbnail")

                if not img_url:
                    log.warning("Entry #%d da URL topilmadi", entry_idx)
                    continue

                # Darhol yuklab olish — retry bilan
                downloaded = False
                for attempt in range(3):
                    try:
                        # Har urinishda URL ni QAYTA olamiz (eskirgan bo'lsa)
                        if attempt > 0:
                            log.info("Qayta urinish #%d entry #%d", attempt, entry_idx)
                            fresh_info = ydl.extract_info(url, download=False)
                            fresh_entries = list(
                                fresh_info.get("entries") or [fresh_info]
                            )
                            if entry_idx < len(fresh_entries):
                                fresh_entry = fresh_entries[entry_idx]
                                fresh_thumbs = fresh_entry.get("thumbnails") or []
                                if fresh_thumbs:
                                    best = max(
                                        fresh_thumbs,
                                        key=lambda t: (t.get("width") or 0) * (t.get("height") or 0)
                                    )
                                    img_url = best.get("url") or img_url
                                else:
                                    img_url = fresh_entry.get("thumbnail") or img_url

                        r = client.get(img_url, headers=headers)

                        # HTML redirect tekshiruvi
                        ct = r.headers.get("content-type", "")
                        if "text/html" in ct:
                            log.warning("HTML redirect keldi entry #%d attempt #%d", entry_idx, attempt)
                            time.sleep(1)
                            continue

                        r.raise_for_status()

                        if len(r.content) < 5 * 1024:
                            log.warning("Rasm juda kichik entry #%d", entry_idx)
                            break

                        # Kengaytma aniqlash
                        ct_clean = ct.split(";")[0].strip()
                        guessed = mimetypes.guess_extension(ct_clean)
                        ext = guessed.lstrip(".") if guessed else "jpg"
                        if ext in ("jpe", "jpeg"):
                            ext = "jpg"

                        out_path = TMP_DIR / f"img_{user_id}_{uid}_{entry_idx}.{ext}"
                        out_path.write_bytes(r.content)
                        saved.append(str(out_path))
                        log.info(
                            "Rasm saqlandi: %s (%.1f KB)",
                            out_path.name, len(r.content) / 1024
                        )
                        downloaded = True
                        break

                    except httpx.HTTPStatusError as e:
                        log.warning(
                            "HTTP xato %d entry #%d attempt #%d",
                            e.response.status_code, entry_idx, attempt
                        )
                        time.sleep(1.5)

                    except Exception as e:
                        log.warning("Xato entry #%d attempt #%d: %s", entry_idx, attempt, e)
                        time.sleep(1)

                if not downloaded:
                    log.error("Entry #%d yuklab bo'lmadi (3 urinishdan keyin)", entry_idx)

    if not saved:
        raise FileNotFoundError("Rasmlar yuklab bo'lmadi")

    log.info("%d rasm tayyor | %.1fs", len(saved), time.time() - t0)
    return saved


async def _url_gc():
    while True:
        await asyncio.sleep(600)
        now     = time.time()
        expired = [k for k, v in list(url_store.items()) if now - v["ts"] > URL_TTL]
        for k in expired:
            url_store.pop(k, None)
        if expired:
            log.info("GC: %d URL o'chirildi", len(expired))

async def on_startup():
    asyncio.create_task(_url_gc())
    log.info("Bot ishga tushdi")

dp.startup.register(on_startup)

@dp.message(F.text == "/start")
async def start_cmd(message: types.Message):
    await message.answer(
        "👋 <b>Salom!</b>\n\n"
        "YouTube, Instagram, TikTok yoki boshqa\n"
        "saytlardan link yuboring — yuklab beraman.\n\n"
        "📥 <b>Video</b> — video yuklash\n"
        "🎵 <b>Audio</b> — audio yuklash\n"
        "🖼 <b>Rasm</b>  — rasm(lar) yuklash\n"
        "ℹ️ <b>Yordam</b> — ko'rsatmalar",
        parse_mode="HTML",
        reply_markup=main_kb(),
    )

@dp.message(F.text.in_({"📥 Video", "🎵 Audio", "🖼 Rasm"}))
async def mode_btn(message: types.Message):
    icons = {"📥 Video": "🎬", "🎵 Audio": "🎵", "🖼 Rasm": "🖼"}
    icon  = icons[message.text]
    await message.answer(
        f"{icon} Yuklamoqchi bo'lgan linkni yuboring:",
        reply_markup=main_kb(),
    )
    url_store[message.from_user.id] = {
        "url":  None,
        "ts":   time.time(),
        "mode": {"📥 Video": "v", "🎵 Audio": "a", "🖼 Rasm": "i"}[message.text],
    }

@dp.message(F.text == "ℹ️ Yordam")
async def help_btn(message: types.Message):
    await message.answer(
        "📖 <b>Qo'llanma</b>\n\n"
        "1. Kerakli tugmani bosing\n"
        "2. Linkni yuboring\n"
        "3. Fayl(lar) tayyor bo'lgach yuboriladi\n\n"
        "<b>Cheklovlar:</b>\n"
        "• Maksimal fayl: 50 MB\n"
        "• Bir vaqtda 5 ta yuklash\n"
        "• Rasmlar: max 10 ta\n\n"
        "<b>Ishlaydi:</b> YouTube, Instagram,\n"
        "TikTok, Facebook, Twitter/X va boshqalar",
        parse_mode="HTML",
        reply_markup=main_kb(),
    )

@dp.message(F.text.regexp(r"^https?://\S{4,2000}$"))
async def handle_link(message: types.Message):
    user_id = message.from_user.id
    url     = message.text.strip()

    existing = url_store.get(user_id)

    if existing and existing.get("mode") and existing.get("url") is None:
        mode = existing["mode"]
        url_store[user_id] = {"url": url, "ts": time.time(), "mode": mode}
        await _dispatch_download(message, user_id, url, mode)
        return

    url_store[user_id] = {"url": url, "ts": time.time(), "mode": None}
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🎬 Video", callback_data=f"v_{user_id}"),
        InlineKeyboardButton(text="🎵 Audio", callback_data=f"a_{user_id}"),
        InlineKeyboardButton(text="🖼 Rasm",  callback_data=f"i_{user_id}"),
    ]])
    await message.reply("📌 Format tanlang:", reply_markup=kb)

async def _dispatch_download(
    source: types.Message | types.CallbackQuery,
    owner_id: int,
    url: str,
    mode: str,
):
    msg = source if isinstance(source, types.Message) else source.message

    labels = {"v": "🎬 Video", "a": "🎵 Audio", "i": "🖼 Rasm"}
    status_msg = await msg.answer(f"{labels[mode]} yuklanmoqda... ⏳")
    file_path  = None
    img_paths: list[str] = []

    try:
        async with semaphore:
            loop = asyncio.get_running_loop()

            if mode in ("v", "a"):
                file_path = await loop.run_in_executor(
                    executor, download_media, url, owner_id, mode
                )
            else:
                img_paths = await loop.run_in_executor(
                    executor, extract_images, url, owner_id
                )

        if mode in ("v", "a"):
            size = os.path.getsize(file_path)
            if size > MAX_SIZE:
                await _safe_edit(status_msg, f"❌ Fayl {size // 1024 // 1024} MB — 50 MB dan katta.")
                return

            await _safe_edit(status_msg, "📤 Yuborilmoqda...")
            inp = FSInputFile(file_path)
            if mode == "v":
                await msg.answer_video(video=inp, caption="✅ @Navo_ai_bot")
            else:
                await msg.answer_audio(audio=inp, caption="✅ @Navo_ai_bot")

        else:
            await _safe_edit(status_msg, f"📤 {len(img_paths)} ta rasm yuborilmoqda...")

            if len(img_paths) == 1:
                await msg.answer_photo(
                    photo=FSInputFile(img_paths[0]),
                    caption="✅ @Navo_ai_bot"
                )
            else:
                chunk_size = 10
                for chunk_start in range(0, len(img_paths), chunk_size):
                    chunk = img_paths[chunk_start:chunk_start + chunk_size]
                    media = MediaGroupBuilder()
                    for i, p in enumerate(chunk):
                        if i == len(chunk) - 1:
                            media.add_photo(FSInputFile(p), caption="✅ @Navo_ai_bot")
                        else:
                            media.add_photo(FSInputFile(p))
                    await msg.answer_media_group(media=media.build())

        url_store.pop(owner_id, None)
        await _safe_delete(status_msg)

    except Exception as e:
        log.error("Xato | user=%s: %s", owner_id, e, exc_info=True)
        await _safe_edit(status_msg, f"❌ Xato: {str(e)[:200]}")
    finally:
        _cleanup(file_path)
        _cleanup_list(img_paths)

@dp.callback_query(F.data.regexp(r"^[vai]_\d+$"))
async def process_callback(callback: types.CallbackQuery):
    mode, owner_str = callback.data.split("_", 1)
    owner_id        = int(owner_str)

    if callback.from_user.id != owner_id:
        return await callback.answer("❗ Bu sizning so'rovingiz emas!", show_alert=True)

    entry = url_store.get(owner_id)
    if not entry or not entry.get("url"):
        await callback.answer("⌛ Link eskirgan, qaytadan yuboring.", show_alert=True)
        await _safe_delete(callback.message)
        return

    await callback.answer()
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

    await _dispatch_download(callback, owner_id, entry["url"], mode)

async def main():
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        log.info("Bot to'xtatildi.")
