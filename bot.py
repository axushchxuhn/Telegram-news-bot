import os
import time
import requests
import feedparser
import schedule
from telegram import Bot

# ====== CONFIG (ENVIRONMENT VARIABLES SE LE RAHE HAIN) =======

# In values ko code me nahi, Railway me "Variables" me set karoge:
# BOT_TOKEN  -> BotFather se
# CHANNEL_ID -> @chxuhan
# AI_KEY     -> OpenAI API key

BOT_TOKEN = os.environ.get("BOT_TOKEN")
CHANNEL_ID = os.environ.get("CHANNEL_ID")
AI_KEY = os.environ.get("AI_KEY")

if not BOT_TOKEN or not CHANNEL_ID or not AI_KEY:
    raise ValueError(
        "BOT_TOKEN, CHANNEL_ID, AI_KEY me se koi missing hai. "
        "Railway ke Variables me teeno set karo."
    )

bot = Bot(token=BOT_TOKEN)

# ====== NEWS SOURCES (WORLDWIDE) =======
RSS_LINKS = [
    "https://news.google.com/news/rss?hl=en-IN&gl=IN&ceid=IN:en",
    "http://feeds.bbci.co.uk/news/world/rss.xml",
    "https://www.reutersagency.com/feed/?best-topics=world&post_type=best"
]

# ========== AI SUMMARY (OpenAI API via requests) ==========
def summarize(text: str) -> str:
    url = "https://api.openai.com/v1/chat/completions"

    system_prompt = (
        "Tum ek Telegram channel ke liye world news summarizer ho. "
        "Har news ko 2-3 line ki short Hinglish me batao, "
        "simple words, 1-2 emoji, aur end me 3 short hashtags lagao. "
        "Bahut lamba mat likho."
    )

    payload = {
        "model": "gpt-4.1-mini",
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": text},
        ],
        "max_tokens": 120,
        "temperature": 0.7,
    }

    headers = {
        "Authorization": f"Bearer {AI_KEY}",
        "Content-Type": "application/json",
    }

    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]["content"]
    except Exception as e:
        print("❌ Error in summarize:", e)
        return "News summarize karte waqt error aaya 😅"


# ========== FETCH NEWS ==========
def get_latest_news():
    all_news = []

    for link in RSS_LINKS:
        feed = feedparser.parse(link)
        # har source se top 3
        for entry in feed.entries[:3]:
            all_news.append(f"{entry.title} - {entry.link}")

    # total 3 hi lenge, 10–10 min me bahut spam na ho
    return all_news[:3]


# ========== POST NEWS TO CHANNEL ==========
def post_news():
    print("🔄 New cycle: news fetch + post karna start...")
    try:
        news_list = get_latest_news()
        if not news_list:
            print("❌ Koi news nahi mili.")
            return

        for news in news_list:
            try:
                short = summarize(news)
                post = f"🌍 *World Update*\n\n{short}"
                bot.send_message(chat_id=CHANNEL_ID, text=post, parse_mode="Markdown")
                print("✅ Post ki gayi:", short[:80])
                time.sleep(5)  # messages ke beech thoda gap
            except Exception as e:
                print("❌ Ek news post karte time error:", e)
                time.sleep(2)

    except Exception as e:
        print("❌ post_news top-level error:", e)


def main():
    # ====== SCHEDULE: HAR 10 MINUTE ME =========
    schedule.every(10).minutes.do(post_news)

    print("🤖 Auto News Bot 24/7 mode me chal raha hai (Railway pe)...")
    while True:
        schedule.run_pending()
        time.sleep(1)


if __name__ == "__main__":
    main()
