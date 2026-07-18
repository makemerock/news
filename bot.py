"""
Бот для Telegram-канала о ремонте/стройке/даче/дизайне.

Что делает при каждом запуске:
1. Проходит по списку RSS-источников (SOURCES ниже).
2. Находит статьи, которых ещё не было (проверка по SQLite).
3. Берёт текст статьи, делает рерайт через бесплатный Gemini API.
4. Генерирует тематическую картинку через Pollinations.ai (бесплатно, без ключа).
5. Публикует пост (картинка + текст) в Telegram-канал.
6. Запоминает статью в базе, чтобы не постить повторно.

Всё бесплатно: Gemini API (free tier), Pollinations.ai (free), GitHub Actions (cron).
"""

import os
import sqlite3
import time
import urllib.parse
import feedparser
import requests
from bs4 import BeautifulSoup

# ---------- НАСТРОЙКИ ----------

# RSS-источники (могут пересыхать со временем — держим как дополнительные)
SOURCES = [
    "http://pro-remont.com/feed.rss",
    "http://www.obstanovka.com/feed/",
]

# Публичные Telegram-каналы по теме — читаем через их веб-версию (t.me/s/...), без токенов.
# Проверенный и активный: DOMEO (@domeoru) — дизайн/ремонт/недвижимость, 537K подписчиков, постит ежедневно.
# Добавляй новые каналы сюда просто по имени (без @ и без t.me/) — код менять не нужно.
TELEGRAM_SOURCE_CHANNELS = [
    "domeoru",
]

# Сколько новых постов публиковать за один запуск скрипта (чтобы не спамить разом)
MAX_POSTS_PER_RUN = 3

DB_PATH = "posted.db"

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHANNEL_ID = os.environ["TELEGRAM_CHANNEL_ID"]  # например "@my_remont_channel" или "-100123456789"
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]

GEMINI_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    f"gemini-2.5-flash:generateContent?key={GEMINI_API_KEY}"
)

# Не постить статьи старше этого количества дней (фильтр от "мёртвых"/архивных RSS)
MAX_ARTICLE_AGE_DAYS = 30


# ---------- БАЗА ДАННЫХ ----------

def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS posted (
               link TEXT PRIMARY KEY,
               posted_at INTEGER
           )"""
    )
    conn.commit()
    return conn


def already_posted(conn, link: str) -> bool:
    row = conn.execute("SELECT 1 FROM posted WHERE link = ?", (link,)).fetchone()
    return row is not None


def mark_posted(conn, link: str):
    conn.execute(
        "INSERT INTO posted (link, posted_at) VALUES (?, ?)", (link, int(time.time()))
    )
    conn.commit()


# ---------- СБОР СТАТЕЙ ----------

def fetch_telegram_channel_posts(channel: str):
    """Читает последние посты публичного Telegram-канала через его веб-версию (без токенов и авторизации)."""
    url = f"https://t.me/s/{channel}"
    try:
        resp = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
    except Exception as e:
        print(f"Не удалось прочитать канал {channel}: {e}")
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    posts = []
    for msg_div in soup.find_all("div", class_="tgme_widget_message", attrs={"data-post": True}):
        post_id = msg_div["data-post"]  # например "domeoru/11104"
        link = f"https://t.me/{post_id}"

        text_div = msg_div.find("div", class_="tgme_widget_message_text")
        if not text_div:
            continue  # пост без текста (только фото/видео) — пропускаем
        text = text_div.get_text("\n", strip=True)
        if len(text) < 200:
            continue  # слишком короткая подпись (например, просто "смотрите видео") — не тянет на пост

        time_tag = msg_div.find("time")
        published_ts = None
        if time_tag and time_tag.get("datetime"):
            try:
                from datetime import datetime
                published_ts = datetime.fromisoformat(time_tag["datetime"]).timestamp()
            except Exception:
                pass

        title = text.split("\n")[0][:80]  # первая строка поста как заголовок
        posts.append(
            {"title": title, "link": link, "summary": text, "published_ts": published_ts}
        )

    return posts


def fetch_new_articles(conn):
    """Собирает новые и свежие статьи из RSS-источников и Telegram-каналов."""
    import calendar

    new_articles = []
    cutoff = time.time() - MAX_ARTICLE_AGE_DAYS * 86400

    for feed_url in SOURCES:
        try:
            feed = feedparser.parse(feed_url)
        except Exception as e:
            print(f"Не удалось прочитать {feed_url}: {e}")
            continue

        fresh_in_this_feed = 0
        for entry in feed.entries:
            link = entry.get("link")
            title = entry.get("title", "")
            if not link or already_posted(conn, link):
                continue

            published_struct = entry.get("published_parsed") or entry.get("updated_parsed")
            if published_struct:
                published_ts = calendar.timegm(published_struct)
                if published_ts < cutoff:
                    continue
            fresh_in_this_feed += 1

            summary = entry.get("summary", "") or entry.get("description", "")
            new_articles.append({"title": title, "link": link, "summary": summary})

        print(f"{feed_url}: свежих новых статей — {fresh_in_this_feed}")

    for channel in TELEGRAM_SOURCE_CHANNELS:
        posts = fetch_telegram_channel_posts(channel)
        fresh_in_this_channel = 0
        for post in posts:
            if already_posted(conn, post["link"]):
                continue
            if post["published_ts"] and post["published_ts"] < cutoff:
                continue
            fresh_in_this_channel += 1
            new_articles.append(post)

        print(f"Telegram @{channel}: свежих новых постов — {fresh_in_this_channel}")

    return new_articles


def fetch_full_text(url: str, fallback: str) -> str:
    """Пытается вытащить основной текст статьи со страницы. Если не выходит — берёт summary из RSS."""
    try:
        resp = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        paragraphs = soup.find_all("p")
        text = "\n".join(p.get_text(strip=True) for p in paragraphs if len(p.get_text(strip=True)) > 40)
        if len(text) > 200:
            return text[:6000]  # ограничим, чтобы не раздувать промпт
    except Exception as e:
        print(f"Не удалось загрузить полный текст {url}: {e}")

    # fallback: чистим html из summary
    return BeautifulSoup(fallback, "html.parser").get_text(strip=True)


# ---------- РЕРАЙТ ЧЕРЕЗ GEMINI ----------

def rewrite_article(title: str, text: str) -> str:
    prompt = (
        "Ты редактор Telegram-канала про ремонт, строительство, дачи и дизайн интерьера. "
        "Перепиши статью своими словами: сохрани полезные факты и советы, но полностью "
        "измени структуру предложений и формулировки, чтобы текст был уникальным. "
        "Игнорируй в исходном тексте рекламные вставки, ссылки на ботов/каналы, призывы "
        "поставить реакцию или подписаться — используй только содержательную часть про ремонт/дизайн. "
        "Сделай пост живым и понятным, без канцелярита. "
        "Формат: 1) короткий цепляющий заголовок с эмодзи, 2) текст поста 5-8 предложений, "
        "3) в конце 3-5 хэштегов по теме. Не упоминай исходный источник.\n\n"
        f"Заголовок статьи: {title}\n\nТекст статьи:\n{text}"
    )

    body = {"contents": [{"parts": [{"text": prompt}]}]}

    max_retries = 3
    for attempt in range(max_retries):
        resp = requests.post(GEMINI_URL, json=body, timeout=30)
        if resp.status_code == 429:
            wait = 20 * (attempt + 1)  # 20с, 40с, 60с
            print(f"Лимит запросов Gemini (429), жду {wait} секунд и пробую снова...")
            time.sleep(wait)
            continue
        resp.raise_for_status()
        data = resp.json()
        return data["candidates"][0]["content"]["parts"][0]["text"].strip()

    raise RuntimeError("Gemini: превышен лимит запросов после нескольких попыток")


# ---------- ГЕНЕРАЦИЯ КАРТИНКИ ----------

def generate_image(title: str) -> bytes:
    """Pollinations.ai — бесплатная генерация картинки по промпту, без API-ключа."""
    prompt = f"cozy interior design photo, {title}, realistic, high quality, warm lighting"
    encoded = urllib.parse.quote(prompt)
    url = f"https://image.pollinations.ai/prompt/{encoded}?width=1024&height=768&nologo=true"
    resp = requests.get(url, timeout=60)
    resp.raise_for_status()
    return resp.content


# ---------- ПУБЛИКАЦИЯ В TELEGRAM ----------

def post_to_telegram(caption: str, image_bytes: bytes):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
    files = {"photo": ("image.jpg", image_bytes)}
    data = {"chat_id": TELEGRAM_CHANNEL_ID, "caption": caption[:1024]}  # Telegram лимит подписи
    resp = requests.post(url, data=data, files=files, timeout=30)
    resp.raise_for_status()


# ---------- ОСНОВНОЙ ЦИКЛ ----------

def main():
    conn = init_db()
    articles = fetch_new_articles(conn)
    print(f"Найдено новых статей: {len(articles)}")

    posted_count = 0
    for article in articles:
        if posted_count >= MAX_POSTS_PER_RUN:
            break

        try:
            if article["link"].startswith("https://t.me/"):
                full_text = article["summary"]  # текст поста уже полный, доп. запрос не нужен
            else:
                full_text = fetch_full_text(article["link"], article["summary"])
            rewritten = rewrite_article(article["title"], full_text)
            image_bytes = generate_image(article["title"])
            post_to_telegram(rewritten, image_bytes)
            mark_posted(conn, article["link"])
            posted_count += 1
            print(f"Опубликовано: {article['title']}")
            time.sleep(5)  # небольшая пауза между постами
        except Exception as e:
            print(f"Ошибка при обработке '{article['title']}': {e}")
            continue

    conn.close()
    print(f"Готово. Опубликовано постов: {posted_count}")


if __name__ == "__main__":
    main()
