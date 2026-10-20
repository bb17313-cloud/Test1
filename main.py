import os
import time
import requests

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
CHAT_ID = str(os.environ.get("CHAT_ID", ""))
BRIDGE_URL = os.environ.get("BRIDGE_URL", "http://localhost:5000/ask") # رابط الجسر المحلي الخاص بك
API = f"https://api.telegram.org/bot{BOT_TOKEN}"

def send_message(chat_id, text, reply_to=None):
    try:
        requests.post(f"{API}/sendMessage", json={
            "chat_id": chat_id,
            "text": text,
            "reply_to_message_id": reply_to
        }, timeout=30)
    except Exception as e:
        print("Telegram send error:", e)

def process_via_bridge(prompt_text):
    try:
        r = requests.post(BRIDGE_URL, json={"prompt": prompt_text}, timeout=60)
        res = r.json()
        if res.get("status") == "success":
            return res.get("result", "⚠️ تم التوليد بنجاح ولكن النص فارغ.")
        else:
            return f"❌ خطأ من الجسر: {res.get('message')}"
    except Exception as e:
        return f"❌ تعذر الاتصال بالجسر المحلي: {str(e)}"

def main():
    print("🤖 البوت يعمل ويراقب الرسائل عبر الجسر...")
    offset = None
    start_time = time.time()
    
    while time.time() - start_time < 1800:
        try:
            params = {"timeout": 10}
            if offset:
                params["offset"] = offset
                
            res = requests.get(f"{API}/getUpdates", params=params, timeout=20).json()
            if res.get("ok") and "result" in res:
                for upd in res["result"]:
                    offset = upd["update_id"] + 1
                    msg = upd.get("message")
                    
                    if msg and str(msg.get("chat", {}).get("id")) == CHAT_ID:
                        text = msg.get("text")
                        mid = msg.get("message_id")
                        
                        if text:
                            print(f"📥 تم استلام: {text}")
                            send_message(CHAT_ID, "⏳ جاري المعالجة عبر المتصفح بدون قيود...", mid)
                            answer = process_via_bridge(text)
                            send_message(CHAT_ID, answer, mid)
        except Exception as e:
            print("Loop error:", e)
            time.sleep(5)

if __name__ == "__main__":
    main()
