import time
import requests

# إعدادات البوت والدردشة
BOT_TOKEN = "YOUR_BOT_TOKEN_HERE"  # ضع توكن البوت هنا
CHAT_ID = "YOUR_CHAT_ID_HERE"      # ضع معرف الشات أو القناة هنا

def get_webull_gainers(market_type="pre"):
    """
    سحب قائمة الأسهم الصاعدة من Webull
    market_type: 'pre' لجلسة Pre-market أو 'post' لجلسة After-hours
    """
    url = f"https://quotes-api.webullfintech.com/api/stock/marketMover/getTopGainers?regionId=6&type={market_type}&count=20"
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
    }
    
    try:
        response = requests.get(url, headers=headers, timeout=10)
        if response.status_code == 200:
            data = response.json()
            gainers_list = data.get("data", [])
            return gainers_list
        else:
            print(f"خطأ في الاتصال: {response.status_code}")
            return []
    except Exception as e:
        print(f"حدث خطأ أثناء جلب البيانات: {e}")
        return []

def format_message(stocks, session_name):
    """تنسيق الرسالة لإرسالها بشكل مرتب"""
    if not stocks:
        return f"⚠️ لا توجد بيانات متاحة حالياً لجلسة {session_name}."
    
    message = f"🚨 **الأسهم المتحركة ({session_name}) - Webull**\n"
    message += "⏰ التحديث اللحظي\n\n"
    
    for idx, stock in enumerate(stocks[:15], 1):
        symbol = stock.get("ticker", {}).get("symbol", "N/A")
        price = stock.get("pPrice", 0.0)
        change_rate = stock.get("changeRate", 0.0)
        
        if change_rate:
            change_pct = float(change_rate) * 100
            sign = "+" if change_pct > 0 else ""
        else:
            change_pct = 0.0
            sign = ""

        message += f"{idx}. **{symbol}** : `{sign}{change_pct:.2f}%` • ${price:.2f}\n"
        
    return message

def send_message(text):
    """إرسال الرسالة إلى تيليجرام"""
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": text,
        "parse_mode": "Markdown"
    }
    try:
        response = requests.post(url, json=payload, timeout=10)
        return response.json()
    except Exception as e:
        print(f"خطأ في إرسال الرسالة: {e}")

def main():
    print("🚀 بدء تشغيل رادار Webull للأسهم المتحركة...")
    
    session = "post" # أو 'pre'
    session_title = "Post-Market / After-hours"
    
    while True:
        print("جاري سحب التحديثات من Webull...")
        stocks = get_webull_gainers(market_type=session)
        
        if stocks:
            msg = format_message(stocks, session_title)
            send_message(msg)
            print("✅ تم إرسال التحديث بنجاح.")
        else:
            print("⚠️ لم يتم العثور على أسهم جديدة أو فشل الجلب.")
            
        time.sleep(180)

if __name__ == "__main__":
    main()
