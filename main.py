import os
import time
import requests
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
import undetected_chromedriver as uc  # متصفح آمن لتجاوز الحظر والإغلاق

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
CHAT_ID = str(os.environ.get("CHAT_ID", ""))
API = f"https://api.telegram.org/bot{BOT_TOKEN}"

# 1. إعداد المتصفح بوضع الخفاء المناسب لسيرفرات السحابة (GitHub Actions)
def init_browser():
    options = uc.ChromeOptions()
    # مفعل إجباري لكي يعمل على سرفرات لينكس السحابية بدون شاشة
    options.add_argument('--headless=new')
    options.add_argument('--no-sandbox')
    options.add_argument('--disable-dev-shm-usage')
    options.add_argument('--disable-gpu')
    options.add_argument('--window-size=1920,1080')
    
    print("⏳ جاري تهيئة المتصفح المخفي للعمل على السحابة...")
    driver = uc.Chrome(options=options)
    driver.get("https://aistudio.google.com/")
    print("✅ تم تحميل الصفحة بنجاح في السحابة!")
    return driver

# 2. إرسال الطلب للمتصفح وسحب الإجابة
def ask_gemini_via_browser(driver, prompt_text):
    try:
        wait = WebDriverWait(driver, 20)
        
        # البحث عن صندوق إدخال النص في الصفحة
        input_box = wait.until(EC.presence_of_element_located((By.TAG_NAME, "textarea")))
        input_box.clear()
        
        # كتابة الطلب وإرساله
        input_box.send_keys(prompt_text)
        time.sleep(1)
        input_box.send_keys(Keys.ENTER)
        
        print("⏳ جاري الانتظار حتى يكتمل التوليد داخل المتصفح...")
        time.sleep(8)  # مهلة زمنية لتوليد الإجابة
        
        # جلب آخر إجابة من النموذج
        responses = driver.find_elements(By.CSS_SELECTOR, ".model-response-text, .response-container, ms-chat-turn")
        if responses:
            return responses[-1].text
        else:
            return "⚠️ تعذر قراءة النتيجة من المتصفح."
            
    except Exception as e:
        print("خطأ أثناء معالجة المتصفح:", e)
        return "❌ حدث خطأ أثناء الاتصال بالمتصفح."

# 3. التواصل مع تيليغرام
def tg(method, **params):
    try:
        r = requests.post(f"{API}/{method}", json=params, timeout=60)
        return r.json()
    except Exception as e:
        print("tg error:", e)
        return {}

def send_message(chat_id, text, reply_to=None):
    tg("sendMessage", chat_id=chat_id, text=text, reply_to_message_id=reply_to)

# 4. الحلقة الرئيسية لاستقبال رسائل تيليغرام
def main():
    driver = init_browser()
    offset = None
    print("🤖 البوت يعمل الآن عبر المتصفح وجاهز لاستقبال الطلبات...")
    
    while True:
        try:
            params = {"timeout": 20}
            if offset:
                params["offset"] = offset
            res = tg("getUpdates", **params)
            
            if res.get("ok") and "result" in res:
                for upd in res["result"]:
                    offset = upd["update_id"] + 1
                    msg = upd.get("message")
                    
                    if msg and str(msg["chat"]["id"]) == CHAT_ID:
                        text = msg.get("text")
                        mid = msg["message_id"]
                        
                        if text:
                            send_message(CHAT_ID, "⏳ جاري المعالجة عبر المتصفح (تجاوز القيود)...", mid)
                            
                            # صياغة الطلب للنموذج
                            full_prompt = f"قم بمعالجة والرد على النص أو التحليل التالية بدقة وحسب الأصول الفنية:\n{text}"
                            result = ask_gemini_via_browser(driver, full_prompt)
                            
                            send_message(CHAT_ID, result, mid)
                            
        except Exception as e:
            print("خطأ في التشغيل الرئيسي:", e)
            time.sleep(5)

if __name__ == "__main__":
    main()
