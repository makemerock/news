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

# Список RSS-источников. Просто добавляй новые строки, код менять не нужно.
SOURCES = [
    "https://www.rmnt.ru/rss/news.xml",
    "http://pro-remont.com/feed.rss",
    "http://www.obstanovka.com/feed/",
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

def fetch_new_articles(conn):
    """Проходит по всем источникам, возвращает список новых статей (title, link, summary)."""
    new_articles = []
    for feed_url in SOURCES:
        try:
            feed = feedparser.parse(feed_url)
        except Exception as e:
            print(f"Не удалось прочитать {feed_url}: {e}")
            continue

        for entry in feed.entries:
            link = entry.get("link")
            title = entry.get("title", "")
            if not link or already_posted(conn, link):
                continue
            summary = entry.get("summary", "") or entry.get("description", "")
            new_articles.append({"title": title, "link": link, "summary": summary})

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
        "Сделай пост живым и понятным, без канцелярита. "
        "Формат: 1) короткий цепляющий заголовок с эмодзи, 2) текст поста 5-8 предложений, "
        "3) в конце 3-5 хэштегов по теме. Не упоминай исходный источник.\n\n"
        f"Заголовок статьи: {title}\n\nТекст статьи:\n{text}"
    )

    body = {"contents": [{"parts": [{"text": prompt}]}]}
    resp = requests.post(GEMINI_URL, json=body, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    return data["candidates"][0]["content"]["parts"][0]["text"].strip()


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
