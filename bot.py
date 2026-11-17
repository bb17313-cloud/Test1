import os
import time
from datetime import datetime
from zoneinfo import ZoneInfo
import requests

# قراءة المتغيرات من إعدادات GitHub Actions أو وضعها مباشرة هنا
BOT_TOKEN = os.getenv("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
CHAT_ID = os.getenv("CHAT_ID", "YOUR_CHAT_ID_HERE")
RUN_SECONDS = int(os.getenv("RUN_SECONDS", "20700"))  # مدة التشغيل بالثواني

def get_current_session():
    """تحديد الجلسة النشطة تلقائياً حسب توقيت السوق الأمريكي (نيويورك)"""
    ny_time = datetime.now(ZoneInfo("America/New_York"))
    current_minutes = ny_time.hour * 60 + ny_time.minute

    # 04:00 AM (240) -> 09:30 AM (570): Pre-market
    if 240 <= current_minutes < 570:
        return "pre", "🌅 Pre-Market (ما قبل الافتتاح)"
    # 09:30 AM (570) -> 04:00 PM (960): Main Session
    elif 570 <= current_minutes < 960:
        return "1d", "🔔 Main Session (الجلسة الرئيسية)"
    # 04:00 PM (960) -> 08:00 PM (1200): After-hours
    elif 960 <= current_minutes < 1200:
        return "post", "🌆 After-Hours (ما بعد الإغلاق)"
    # 08:00 PM (1200) -> 04:00 AM (240): Night / Overnight
    else:
        return "night", "🌙 Night Session (التداول الليلي)"

def get_webull_gainers(market_type):
    """سحب قائمة الأسهم الصاعدة من Webull حسب نوع الجلسة"""
    url = f"https://quotes-api.webullfintech.com/api/stock/marketMover/getTopGainers?regionId=6&type={market_type}&count=20"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
    }
    try:
        response = requests.get(url, headers=headers, timeout=10)
        if response.status_code == 200:
            return response.json().get("data", [])
        print(f"خطأ في الاتصال بـ Webull: {response.status_code}")
        return []
    except Exception as e:
        print(f"خطأ أثناء جلب البيانات: {e}")
        return []

def format_message(stocks, session_title):
    """تنسيق الرسالة وإضافة توقيت السعودية"""
    ksa_time = datetime.now(ZoneInfo("Asia/Riyadh")).strftime("%I:%M %p")
    
    message = f"🚨 **الأسهم الفعالة لجلسة {session_title}**\n"
    message += f"⏰ التحديث: {ksa_time} بتوقيت السعودية\n\n"
    
    for idx, stock in enumerate(stocks[:15], 1):
        ticker_info = stock.get("ticker", {})
        symbol = ticker_info.get("symbol", "N/A")
        
        # اختيار السعر ونسبة التغير المناسبة للجلسة
        price = float(stock.get("pPrice") or stock.get("close") or 0.0)
        change_rate = float(stock.get("pChRatio") or stock.get("changeRate") or 0.0)
        
        change_pct = change_rate * 100
        sign = "+" if change_pct > 0 else ""
        message += f"{idx}. **{symbol}** `{sign}{change_pct:.2f}%` • `${price:.2f}`\n"
        
    return message

def send_message(text):
    """إرسال التحديث إلى البوت"""
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": text,
        "parse_mode": "Markdown"
    }
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        print(f"خطأ في إرسال الرسالة: {e}")

def main():
    print("🚀 بدء تشغيل رادار الجلسات الأربع (Pre / Main / After / Night)...")
    start_time = time.time()
    
    while (time.time() - start_time) < RUN_SECONDS:
        session_code, session_title = get_current_session()
        print(f"جاري فحص جلسة: {session_title} ({session_code})...")
        
        stocks = get_webull_gainers(market_type=session_code)
        
        if stocks:
            msg = format_message(stocks, session_title)
            send_message(msg)
            print("✅ تم إرسال القائمة بنجاح.")
        else:
            print("⚠️ لا توجد بيانات متاحة حالياً لهذه الجلسة.")
            
        time.sleep(180)  # التحديث كل 3 دقائق

if __name__ == "__main__":
    main()
