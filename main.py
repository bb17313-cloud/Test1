import glob
import io
import mimetypes
import os
import re
import subprocess
import sys
import tempfile
import time
import traceback
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from urllib.parse import urlparse, parse_qs, unquote

import requests
from pypdf import PdfReader
from docx import Document

BOT_TOKEN = os.environ["BOT_TOKEN"]
CHAT_ID = str(os.environ["CHAT_ID"])
API = f"https://api.telegram.org/bot{BOT_TOKEN}"
FILE_API = f"https://api.telegram.org/file/bot{BOT_TOKEN}"

# إعدادات النماذج
AI_PROVIDER = os.environ.get("AI_PROVIDER", "gemini").lower()
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")

MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
FALLBACK_MODELS = [m.strip() for m in os.environ.get(
    "GEMINI_FALLBACK_MODELS", "gemini-flash-lite-latest,gemini-3.5-flash-lite").split(",") if m.strip()]
GROQ_MODEL = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")

RUN_SECONDS = int(os.environ.get("RUN_SECONDS", "20400"))
SEC_USER_AGENT = os.environ.get("SEC_USER_AGENT", "TranslatorBot contact@example.com")
URL_RE = re.compile(r"https?://\S+")

LAST = {"title": None, "text": None, "kind": "doc"}
STATE = {"mode": None, "hist": []}

HELP = (
    "👋 أهلاً بك!\n\n"
    "📝 أرسل أي نص أو تحليل أو رابط شارت ← سأقوم بمعالجته عبر الـ API فوراً وبدون مشاكل متصفح.\n"
    "/start - المساعدة\n"
    "/new - إعادة ضبط الحالة\n"
)


def tg(method, **params):
    try:
        r = requests.post(f"{API}/{method}", json=params, timeout=60)
        return r.json()
    except Exception as e:
        print("tg error:", method, e)
        return {}


def send(chat_id, text, reply_to=None):
    if len(text) > 4000:
        text = text[:3900] + "\n\n...(تم اختصار الرد لطوله)"
    tg("sendMessage", chat_id=chat_id, text=text, reply_to_message_id=reply_to)


def call_gemini(system, text):
    headers = {"x-goog-api-key": GEMINI_API_KEY}
    body = {
        "system_instruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": text}]}],
    }
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent"
    try:
        r = requests.post(url, json=body, headers=headers, timeout=60)
        res = r.json()
        cands = res.get("candidates", [])
        if cands:
            parts = cands[0].get("content", {}).get("parts", [])
            return "".join(p.get("text", "") for p in parts).replace("**", "").strip()
    except Exception as e:
        print("Gemini API error:", e)
    return "⚠️ تعذر المعالجة عبر النموذج حالياً."


def handle(msg):
    chat_id = msg["chat"]["id"]
    if str(chat_id) != CHAT_ID:
        return
    mid = msg["message_id"]
    text = msg.get("text")

    try:
        if text:
            if text.startswith("/start"):
                send(chat_id, HELP, mid)
            elif text.startswith("/new"):
                send(chat_id, "✅ تم إعادة ضبط الحالة.", mid)
            else:
                send(chat_id, "⏳ جاري المعالجة الفورية...", mid)
                system_prompt = "أنت مساعد مالي وفني محترف للأسواق. أجب بدقة واحترافية باللغة العربية."
                result = call_gemini(system_prompt, text)
                send(chat_id, result, mid)
    except Exception as e:
        print(traceback.format_exc())
        send(chat_id, f"❌ حدث خطأ: {str(e)[:200]}", mid)


def main():
    end = time.time() + RUN_SECONDS
    offset = None
    print("🤖 البوت يعمل الآن بنجاح عبر الـ API المباشر...")
    while time.time() < end:
        params = {"timeout": 20}
        if offset:
            params["offset"] = offset
        res = tg("getUpdates", **params)
        if not res.get("ok", True) or "result" not in res:
            time.sleep(5)
            continue
        for upd in res["result"]:
            offset = upd["update_id"] + 1
            if "message" in upd:
                handle(upd["message"])


if __name__ == "__main__":
    main()
