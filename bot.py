import os
import time
import json
import html
import logging
import tempfile
from datetime import datetime, timedelta
from threading import Thread

import feedparser
import schedule
import requests
from gtts import gTTS
from flask import Flask
from telegram import Bot
from telegram.utils.request import Request


# ========= ENVIRONMENT CONFIG =========

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID")
SELF_PING_URL = os.getenv("SELF_PING_URL")  # e.g. https://your-bot.onrender.com

# DeepSeek (optional – key laga ke chhod do, balance hoga to AI chalega)
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
DEEPSEEK_API_URL = "https://api.deepseek.com/v1/chat/completions"
DEEPSEEK_MODEL = "deepseek-chat"

if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHANNEL_ID:
    raise RuntimeError("Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHANNEL_ID!")


# ========= GLOBAL SETTINGS =========

RSS_LINKS = [
    "https://news.google.com/rss?hl=en-IN&gl=IN&ceid=IN:en",
    "https://feeds.reuters.com/reuters/worldNews",
    "https://feeds.bbci.co.uk/news/world/rss.xml",
]

NEWS_PER_RUN = 5        # har 15 min me max 5 news
sent_ids = set()        # duplicate rokne ke liye

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

request = Request(con_pool_size=8)
bot = Bot(token=TELEGRAM_BOT_TOKEN, request=request)
app = Flask(__name__)


# ========= SHORT URL =========

def short_url(url: str) -> str:
    try:
        r = requests.get(
            "https://tinyurl.com/api-create.php",
            params={"url": url},
            timeout=10,
        )
        if r.status_code == 200:
            return r.text.strip()
    except Exception:
        pass
    return url


# ========= AI SUMMARY (AUTO MODE) =========

def ai_summarize(title: str, description: str, link: str):
    """
    AUTO SYSTEM:
    1) Try DeepSeek (agar key hai)
    2) Fail ho to FREE simple summary
    """

    system_prompt = """
Tum ek professional news assistant ho.

Har news ke liye SIRF yeh JSON return karo:

{
  "summary_en": "...",
  "voice_hi": "...",
  "hashtags": "..."
}

Rules:
- summary_en: 2-3 short English lines.
- voice_hi: Hindi me 1-2 line news reporter tone.
- hashtags: 3–4 tags.
- JSON ke bahar kuch mat likho.
"""

    user_text = f"Title: {title}\n\nDescription: {description}\n\nLink: {link}"

    # 1) DeepSeek AI try karega (agar key lagi hai)
    if DEEPSEEK_API_KEY:
        try:
            payload = {
                "model": DEEPSEEK_MODEL,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_text},
                ],
                "max_tokens": 200,
                "temperature": 0.7,
            }

            res = requests.post(
                DEEPSEEK_API_URL,
                headers={
                    "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=20,
            )

            data = res.json()
            raw = data["choices"][0]["message"]["content"]
            obj = json.loads(raw)

            return (
                obj.get("summary_en", ""),
                obj.get("voice_hi", ""),
                obj.get("hashtags", "#WorldNews #Breaking #Update"),
            )

        except Exception as e:
            logging.error(f"DeepSeek failed → {e}")

    # 2) FREE fallback (no AI / key khatam / error)
    logging.warning("Using FREE fallback summary (no AI).")

    short = (description or title).strip()
    if len(short) > 200:
        short = short[:200].rsplit(" ", 1)[0] + "..."

    summary_en = f"{title}\n\n{short}"
    voice_hi = f"Aaj ki khaas khabar: {title}"
    hashtags = "#Breaking #WorldNews #Update"

    return summary_en, voice_hi, hashtags


# ========= VOICE MAKER =========

def make_voice(text: str):
    if not text or not text.strip():
        return None

    temp = tempfile.NamedTemporaryFile(delete=False, suffix=".mp3")
    temp.close()
    try:
        tts = gTTS(text=text, lang="hi")
        tts.save(temp.name)
        return temp.name
    except Exception:
        return None


# ========= FETCH NEWS (WITH IMAGE) =========

def extract_image_from_entry(e) -> str | None:
    """
    RSS entry se image URL nikalne ki koshish.
    Har feed ka structure alag hota hai, isliye kuch common fields check karte hain.
    """
    # media_content
    try:
        media_content = getattr(e, "media_content", None)
        if media_content and len(media_content) > 0:
            url = media_content[0].get("url")
            if url:
                return url
    except Exception:
        pass

    # media_thumbnail
    try:
        media_thumb = getattr(e, "media_thumbnail", None)
        if media_thumb and len(media_thumb) > 0:
            url = media_thumb[0].get("url")
            if url:
                return url
    except Exception:
        pass

    # links me image-type enclosure
    try:
        for link in getattr(e, "links", []):
            if link.get("type", "").startswith("image/"):
                return link.get("href")
    except Exception:
        pass

    return None


def fetch_news():
    entries = []

    for url in RSS_LINKS:
        try:
            feed = feedparser.parse(url)
            for e in feed.entries[:5]:
                eid = getattr(e, "id", None) or getattr(e, "link", None)
                if not eid or eid in sent_ids:
                    continue

                image_url = extract_image_from_entry(e)

                entries.append({
                    "id": eid,
                    "title": getattr(e, "title", ""),
                    "link": getattr(e, "link", ""),
                    "summary": getattr(e, "summary", "") or getattr(e, "description", ""),
                    "image": image_url,
                })
        except Exception as ex:
            logging.error(f"RSS fetch error from {url}: {ex}")

    # latest first
    return entries[::-1]


# ========= PREMIUM MESSAGE FORMAT =========

def format_message(title: str, summary: str, link: str, hashtags: str) -> str:
    safe_title = html.escape(title)
    safe_summary = html.escape(summary)

    base_tags = hashtags or "#WorldNews #Breaking #Update"
    growth_tags = "#TopStories #GlobalNews"
    safe_tags = html.escape(base_tags + " " + growth_tags)

    ist = datetime.utcnow() + timedelta(hours=5, minutes=30)
    time_str = ist.strftime("%d %b %Y | %I:%M %p IST")

    short = short_url(link)

    msg = (
        f"🚨 <b>International Breaking News</b>\n"
        f"📅 <i>{time_str}</i>\n\n"
        f"📰 <b>{safe_title}</b>\n\n"
        f"{safe_summary}\n\n"
        f"🔗 Full Story: <a href=\"{short}\">Read here</a>\n\n"
        f"{safe_tags}\n\n"
        f"<i>Powered by @Axshchxhan</i>"
    )

    return msg


# ========= POST NEWS (TEXT + IMAGE + AUDIO) =========

def post_news():
    logging.info("Fetching fresh news…")
    entries = fetch_news()

    count = 0
    for e in entries:
        if count >= NEWS_PER_RUN:
            break

        news_id = e["id"]
        title = e["title"]
        link = e["link"]
        desc = e["summary"]
        image_url = e["image"]

        summary, voice, tags = ai_summarize(title, desc, link)
        if not summary:
            sent_ids.add(news_id)
            continue

        msg = format_message(title, summary, link, tags)

        # 1) Agar image hai to pehle photo bhejo (short caption ke saath)
        if image_url:
            try:
                bot.send_photo(
                    chat_id=TELEGRAM_CHANNEL_ID,
                    photo=image_url,
                    caption=f"📰 {title}",
                    parse_mode="HTML",
                )
            except Exception as ex:
                logging.error(f"Image send error: {ex}")

        # 2) Premium text message
        bot.send_message(
            chat_id=TELEGRAM_CHANNEL_ID,
            text=msg,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )

        # 3) Hindi audio
        if voice:
            audio = make_voice(voice)
            if audio:
                try:
                    with open(audio, "rb") as f:
                        bot.send_audio(
                            chat_id=TELEGRAM_CHANNEL_ID,
                            audio=f,
                            caption="🎙 हिंदी ऑडियो न्यूज़",
                        )
                except Exception as ex:
                    logging.error(f"Audio send error: {ex}")
                try:
                    os.remove(audio)
                except Exception:
                    pass

        sent_ids.add(news_id)
        count += 1
        time.sleep(2)


# ========= KEEP ALIVE =========

def ping_self():
    if not SELF_PING_URL:
        return
    try:
        requests.get(SELF_PING_URL, timeout=8)
        logging.info("Ping OK")
    except Exception as e:
        logging.warning(f"Ping failed: {e}")


# ========= STARTUP DEMO =========

def send_startup():
    try:
        ist = datetime.utcnow() + timedelta(hours=5, minutes=30)
        time_str = ist.strftime("%d %b %Y | %I:%M %p IST")

        msg = (
            "🟢 <b>Ayush News Bot Updated</b>\n"
            f"🗓 <i>{time_str}</i>\n\n"
            "Demo: Bot सफलतापूर्वक चालू हो चुका है.\n"
            "Ab har 15 minute me latest international news + image + Hindi audio milegi.\n\n"
            "#Update #LiveBot\n"
            "Powered by @Axshchxhan"
        )

        bot.send_message(
            chat_id=TELEGRAM_CHANNEL_ID,
            text=msg,
            parse_mode="HTML",
        )
    except Exception as e:
        logging.error(f"Startup message error: {e}")


# ========= SCHEDULER =========

def scheduler_loop():
    while True:
        schedule.run_pending()
        time.sleep(1)


@app.route("/")
def home():
    return "Ayush Auto News Bot Running!", 200


def main():
    logging.info("🔥 Ayush Auto News Bot Started!")

    # Startup demo (sirf ek baar)
    send_startup()

    # Har 15 min me news
    schedule.every(15).minutes.do(post_news)

    # Har 5 min me keep-alive ping
    schedule.every(5).minutes.do(ping_self)

    t = Thread(target=scheduler_loop, daemon=True)
    t.start()

    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()
