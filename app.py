import datetime
import requests
from flask import Flask, render_template, request, session, redirect, url_for, flash, jsonify
import openai
import plotly.graph_objs as go
import stripe
from dotenv import load_dotenv
import logging
import time
import yfinance as yf
import pandas as pd
import json
import os
import urllib.parse

# ------------------ Load environment ------------------
load_dotenv()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
SECRET_KEY = os.getenv("SECRET_KEY", "supersecretkey")
FINNHUB_API_KEY = os.getenv("FINNHUB_API_KEY")
# Stripe keys
STRIPE_TEST_SECRET_KEY = os.getenv("STRIPE_TEST_SECRET_KEY")
STRIPE_TEST_PUBLISHABLE_KEY = os.getenv("STRIPE_TEST_PUBLISHABLE_KEY")
STRIPE_LIVE_SECRET_KEY = os.getenv("STRIPE_LIVE_SECRET_KEY")
STRIPE_LIVE_PUBLISHABLE_KEY = os.getenv("STRIPE_LIVE_PUBLISHABLE_KEY")
STRIPE_MODE = os.getenv("STRIPE_MODE", "test").lower()

# Stripe Price IDs
STRIPE_PRICE_IDS = {
    "Free": os.getenv("STRIPE_PRICE_TIER0"),
    "Tier 1": os.getenv("STRIPE_PRICE_TIER1"),
    "Tier 2": os.getenv("STRIPE_PRICE_TIER2"),
    "Tier 3": os.getenv("STRIPE_PRICE_TIER3"),
    "Tier 4": os.getenv("STRIPE_PRICE_TIER4"),
}

if not OPENAI_API_KEY:
    raise RuntimeError("❌ OPENAI_API_KEY not set in .env")
if not FINNHUB_API_KEY:
    raise RuntimeError("❌ FINNHUB_API_KEY not set in .env")

# Set Stripe keys
if STRIPE_MODE == "live":
    STRIPE_SECRET_KEY = STRIPE_LIVE_SECRET_KEY
    STRIPE_PUBLISHABLE_KEY = STRIPE_LIVE_PUBLISHABLE_KEY
else:
    STRIPE_SECRET_KEY = STRIPE_TEST_SECRET_KEY
    STRIPE_PUBLISHABLE_KEY = STRIPE_TEST_PUBLISHABLE_KEY

if not STRIPE_SECRET_KEY or not STRIPE_PUBLISHABLE_KEY:
    raise RuntimeError(f"❌ Stripe keys for mode '{STRIPE_MODE}' not set in .env")

stripe.api_key = STRIPE_SECRET_KEY

# ------------------ Logger setup ------------------
logging.basicConfig(level=logging.INFO, filename='app.log', filemode='a')
logger = logging.getLogger(__name__)

# ------------------ Initialize Flask & OpenAI ------------------
app = Flask(__name__)
app.secret_key = SECRET_KEY
openai.api_key = OPENAI_API_KEY

# ------------------ Stock app config ------------------
industry_mapping = {
    "Technology": "科技業",
    "Financial Services": "金融服務業",
    "Healthcare": "醫療保健業",
    "Consumer Cyclical": "非必需消費品業",
    "Communication Services": "通訊服務業",
    "Energy": "能源業",
    "Industrials": "工業類股",
    "Utilities": "公用事業",
    "Real Estate": "房地產業",
    "Materials": "原物料業",
    "Consumer Defensive": "必需消費品業",
    "Unknown": "未知"
}
IMPORTANT_METRICS = [
    "peTTM", "pb", "roeTTM", "roaTTM", "grossMarginTTM",
    "revenueGrowthTTMYoy", "epsGrowthTTMYoy", "debtToEquityAnnual"
]
METRIC_NAMES_ZH_EN = {
    "pe_ratio": "本益比 (PE TTM)",
    "pb_ratio": "股價淨值比 (PB)",
    "roe_ttm": "股東權益報酬率 (ROE TTM)",
    "roa_ttm": "資產報酬率 (ROA TTM)",
    "gross_margin_ttm": "毛利率 (Gross Margin TTM)",
    "revenue_growth": "營收成長率 (YoY)",
    "eps_growth": "每股盈餘成長率 (EPS Growth YoY)",
    "debt_to_equity": "負債權益比 (Debt to Equity Annual)"
}
QUOTE_FIELDS = {
    "current_price": ("即時股價", "Current Price"),
    "open": ("開盤價", "Open"),
    "high": ("最高價", "High"),
    "low": ("最低價", "Low"),
    "previous_close": ("前收盤價", "Previous Close"),
    "daily_change": ("漲跌幅(%)", "Change Percent"),
    "volume": ("交易量", "Volume")
}

# ------------------ Stripe pricing tiers ------------------
PRICING_TIERS = [
    {"name": "Free", "limit": 50, "price": 0},
    {"name": "Tier 1", "limit": 100, "price": 9.99},
    {"name": "Tier 2", "limit": 200, "price": 19.99},
    {"name": "Tier 3", "limit": 400, "price": 29.99},
    {"name": "Tier 4", "limit": 800, "price": 39.99},
]

# Hard-coded symbol mappings for common cases
SYMBOL_MAPPINGS = {
    "台積電": "2330.TW",
    "TSMC": "2330.TW",
    "台灣積體電路製造": "2330.TW",
    "Taiwan Semiconductor Manufacturing": "2330.TW"
}

# ------------------ Helper functions ------------------
def validate_price_id(price_id, tier_name):
    return bool(price_id)

def get_finnhub_json(endpoint, params):
    url = f"https://finnhub.io/api/v1/{endpoint}"
    params["token"] = FINNHUB_API_KEY
    for _ in range(3):
        try:
            r = requests.get(url, params=params, timeout=5)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            logger.warning(f"[Finnhub Error] {endpoint}: {e}")
            time.sleep(2)
    return {}

def resolve_symbol(input_str):
    input_str = input_str.strip()
    if not input_str:
        return None
    logger.info(f"Resolving symbol: {input_str}")
    
    # Initialize cache if not present
    if "symbol_cache" not in session:
        session["symbol_cache"] = {}
    cache = session["symbol_cache"]

    # Check cache
    if input_str in cache:
        logger.info(f"Cache hit for {input_str}: {cache[input_str]}")
        return cache[input_str]

    # Check hard-coded mappings
    if input_str in SYMBOL_MAPPINGS:
        symbol = SYMBOL_MAPPINGS[input_str]
        profile = get_company_profile(symbol)
        if profile and profile.get('name'):
            cache[input_str] = symbol
            session["symbol_cache"] = cache
            logger.info(f"Resolved {input_str} to {symbol} via mapping")
            return symbol

    # Handle symbols with .TW or .TWO
    if '.' in input_str:
        parts = input_str.rsplit('.', 1)
        symbol = parts[0].upper() + '.' + parts[1].upper()
        profile = get_company_profile(symbol)
        if profile and profile.get('name'):
            cache[input_str] = symbol
            session["symbol_cache"] = cache
            logger.info(f"Resolved {input_str} to {symbol} via direct symbol")
            return symbol

    # Handle numeric stock IDs (Taiwan stocks)
    if input_str.isdigit():
        for suffix in ['.TW', '.TWO']:
            symbol = input_str + suffix
            profile = get_company_profile(symbol)
            if profile and profile.get('name'):
                cache[input_str] = symbol
                session["symbol_cache"] = cache
                logger.info(f"Resolved {input_str} to {symbol} via numeric ID")
                return symbol
        return None

    # Handle company names via Finnhub search
    search_params = {"q": input_str}
    search = get_finnhub_json("search", search_params)
    results = search.get('result', [])
    for res in results:
        symbol = res.get('symbol', '')
        if res.get('type') == 'Common Stock' and (symbol.endswith('.TW') or symbol.endswith('.TWO')):
            profile = get_company_profile(symbol)
            if profile and profile.get('name'):
                cache[input_str] = symbol
                session["symbol_cache"] = cache
                logger.info(f"Resolved {input_str} to {symbol} via Finnhub search")
                return symbol

    # Fallback to OpenAI
    try:
        prompt = (
            f"將以下輸入轉換為台灣股票代號（必須以 .TW 或 .TWO 結尾，例如 2330.TW）。"
            f"如果輸入是 '台積電' 或 'TSMC'，應回傳 '2330.TW'。輸入：{input_str}。僅回覆代號，例如 2330.TW。"
        )
        response = openai.ChatCompletion.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=50,
            temperature=0.2
        )
        suggested_symbol = response['choices'][0]['message']['content'].strip()
        if suggested_symbol.endswith('.TW') or suggested_symbol.endswith('.TWO'):
            profile = get_company_profile(suggested_symbol)
            if profile and profile.get('name'):
                cache[input_str] = suggested_symbol
                session["symbol_cache"] = cache
                logger.info(f"Resolved {input_str} to {suggested_symbol} via OpenAI")
                return suggested_symbol
    except Exception as e:
        logger.warning(f"OpenAI symbol resolution error for {input_str}: {e}")

    # Fallback to Yahoo Finance
    try:
        # Try common Taiwan stock suffixes
        for suffix in ['.TW', '.TWO']:
            ticker = yf.Ticker(input_str + suffix)
            info = ticker.info
            if info.get('symbol') and info['symbol'].endswith(suffix):
                cache[input_str] = info['symbol']
                session["symbol_cache"] = cache
                logger.info(f"Resolved {input_str} to {info['symbol']} via Yahoo Finance")
                return info['symbol']
        # Try searching by name
        search = yf.Ticker("2330.TW")  # Use a known Taiwan stock to anchor search
        info = search.info
        if input_str.lower() in info.get('longName', '').lower() or input_str.lower() in info.get('shortName', '').lower():
            cache[input_str] = "2330.TW"
            session["symbol_cache"] = cache
            logger.info(f"Resolved {input_str} to 2330.TW via Yahoo Finance name search")
            return "2330.TW"
    except Exception as e:
        logger.warning(f"Yahoo Finance symbol resolution error for {input_str}: {e}")

    logger.warning(f"Failed to resolve symbol: {input_str}")
    return None

def get_quote(symbol):
    data = get_finnhub_json("quote", {"symbol": symbol})
    quote = {}
    if data:
        quote = {
            'current_price': round(data.get('c', 'N/A'), 4),
            'open': round(data.get('o', 'N/A'), 4),
            'high': round(data.get('h', 'N/A'), 4),
            'low': round(data.get('l', 'N/A'), 4),
            'previous_close': round(data.get('pc', 'N/A'), 4),
            'daily_change': round(data.get('dp', 'N/A'), 4),
            'volume': 'N/A'
        }
    return quote

def get_metrics(symbol):
    return get_finnhub_json("stock/metric", {"symbol": symbol, "metric": "all"}).get("metric", {})

def filter_metrics(metrics):
    filtered = {}
    metric_map = {
        "peTTM": "pe_ratio",
        "pb": "pb_ratio",
        "roeTTM": "roe_ttm",
        "roaTTM": "roa_ttm",
        "grossMarginTTM": "gross_margin_ttm",
        "revenueGrowthTTMYoy": "revenue_growth",
        "epsGrowthTTMYoy": "eps_growth",
        "debtToEquityAnnual": "debt_to_equity"
    }
    for original_key, new_key in metric_map.items():
        v = metrics.get(original_key)
        if v is not None:
            try:
                v = float(v)
                if "growth" in new_key or "margin" in new_key or "roe" in new_key or "roa" in new_key:
                    filtered[new_key] = f"{v:.2f}%"
                else:
                    filtered[new_key] = round(v, 4)
            except:
                filtered[new_key] = str(v)
    return filtered

def get_recent_news(symbol):
    today = datetime.datetime.now().strftime("%Y-%m-%d")
    past = (datetime.datetime.now() - datetime.timedelta(days=10)).strftime("%Y-%m-%d")
    news = get_finnhub_json("company-news", {"symbol": symbol, "from": past, "to": today})
    if not isinstance(news, list) or len(news) == 0:
        try:
            prompt = (
                f"為股票 {symbol} 生成 5 條最近的虛擬新聞標題和摘要（中英雙語），基於常見市場趨勢。"
                f"格式：標題 (中文) / Title (English) - 摘要 (中文) / Summary (English)"
            )
            response = openai.ChatCompletion.create(
                model="gpt-4o-mini",
                messages=[{"role": "user", "content": prompt}],
                max_tokens=500,
                temperature=0.7
            )
            generated = response['choices'][0]['message']['content'].strip().split('\n')
            news = []
            for line in generated[:10]:
                parts = line.split(' - ', 1)
                if len(parts) == 2:
                    headline = parts[0].strip()
                    summary = parts[1].strip()
                    if headline and summary:
                        news.append({
                            "headline": headline,
                            "summary": summary,
                            "datetime": today,
                            "source": "AI Generated",
                            "url": ""
                        })
        except Exception as e:
            logger.warning(f"OpenAI news generation error for {symbol}: {e}")
            news = []
    else:
        news = sorted(news, key=lambda x: x.get("datetime", 0), reverse=True)[:10]
        for n in news:
            try:
                n["datetime"] = datetime.datetime.utcfromtimestamp(n["datetime"]).strftime("%Y-%m-%d %H:%M")
            except:
                n["datetime"] = "未知時間"
    return news

def get_company_profile(symbol):
    profile = get_finnhub_json("stock/profile2", {"symbol": symbol})
    if not profile or not profile.get('name'):
        try:
            ticker = yf.Ticker(symbol)
            info = ticker.info
            profile = {
                'name': info.get('longName', 'Unknown'),
                'finnhubIndustry': info.get('sector', 'Unknown'),
                'country': info.get('country', 'Unknown')
            }
        except Exception as e:
            logger.warning(f"Yahoo Finance profile error for {symbol}: {e}")
            profile = {}
    return profile

def calculate_rsi(series, period=14):
    delta = series.diff(1)
    gain = delta.where(delta > 0, 0)
    loss = -delta.where(delta < 0, 0)
    avg_gain = gain.rolling(window=period, min_periods=1).mean()
    avg_loss = loss.rolling(window=period, min_periods=1).mean()
    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    return rsi.iloc[-1]

def get_historical_data(symbol):
    df = pd.DataFrame()
    for _ in range(3):
        try:
            df = yf.download(symbol, period="1y", progress=False)
            if not df.empty:
                break
            time.sleep(2)
        except Exception as e:
            logger.warning(f"[YF Historical Error] {symbol}: {e}")
            time.sleep(2)
    if df.empty:
        return pd.DataFrame(), {}
    ma50 = df['Close'].rolling(50).mean().iloc[-1]
    rsi = calculate_rsi(df['Close'])
    ema12 = df['Close'].ewm(span=12, adjust=False).mean()
    ema26 = df['Close'].ewm(span=26, adjust=False).mean()
    macd = ema12.iloc[-1] - ema26.iloc[-1]
    support = df['Low'].tail(20).min()
    resistance = df['High'].tail(20).max()
    volume = df['Volume'].iloc[-1]
    technical = {
        'ma50': round(ma50, 2),
        'rsi': round(rsi, 2),
        'macd': round(macd, 2),
        'support': round(support, 2),
        'resistance': round(resistance, 2),
        'volume': volume
    }
    return df, technical

def get_plot_html(df, symbol):
    if df.empty or 'Close' not in df.columns:
        return "<p class='text-danger'>📊 無法取得股價趨勢圖</p>"
    df_plot = df.tail(7)
    dates = df_plot.index.strftime('%Y-%m-%d').tolist()
    closes = df_plot['Close'].round(2).tolist()
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=dates, y=closes, mode='lines+markers', name='Close Price'))
    fig.update_layout(
        title=f"{symbol} 最近7日收盤價 / 7-Day Closing Price Trend",
        xaxis_title="日期 / Date",
        yaxis_title="收盤價 (USD)",
        template="plotly_white",
        height=400
    )
    return fig.to_html(full_html=False, include_plotlyjs='cdn', default_height="400px", default_width="100%")

# ------------------ Flask routes ------------------
@app.route("/", methods=["GET", "POST"])
def index():
    result = {}
    symbol_input = ""
    symbol = ""
    current_tier_index = session.get("paid_tier", 0)
    current_tier = PRICING_TIERS[current_tier_index]
    request_count = session.get("request_count", 0)
    current_limit = current_tier["limit"]
    current_tier_name = current_tier["name"]
    
    if request.method == "POST":
        if request_count >= current_limit:
            result["error"] = f"已達 {current_tier_name} 等級請求上限，請升級方案"
            return render_template("index.html", result=result, symbol_input=symbol_input,
                                   tiers=PRICING_TIERS, stripe_pub_key=STRIPE_PUBLISHABLE_KEY,
                                   stripe_mode=STRIPE_MODE, request_count=request_count,
                                   current_tier_name=current_tier_name, current_limit=current_limit)
        
        symbol_input = request.form.get("symbol", "").strip()
        if not symbol_input:
            result["error"] = "請輸入股票代號、ID 或名稱 / Please enter a stock symbol, ID, or name"
            return render_template("index.html", result=result, symbol_input=symbol_input,
                                   tiers=PRICING_TIERS, stripe_pub_key=STRIPE_PUBLISHABLE_KEY,
                                   stripe_mode=STRIPE_MODE, request_count=request_count,
                                   current_tier_name=current_tier_name, current_limit=current_limit)

        symbol = resolve_symbol(symbol_input)
        if not symbol:
            result["error"] = f"無法找到股票：{symbol_input} / Stock not found: {symbol_input}"
            return render_template("index.html", result=result, symbol_input=symbol_input,
                                   tiers=PRICING_TIERS, stripe_pub_key=STRIPE_PUBLISHABLE_KEY,
                                   stripe_mode=STRIPE_MODE, request_count=request_count,
                                   current_tier_name=current_tier_name, current_limit=current_limit)

        try:
            session["request_count"] = request_count + 1
            quote = get_quote(symbol)
            metrics = filter_metrics(get_metrics(symbol))
            news = get_recent_news(symbol)
            profile = get_company_profile(symbol)
            industry_en = profile.get("finnhubIndustry", "Unknown")
            industry_zh = industry_mapping.get(industry_en, "未知")
            df, technical = get_historical_data(symbol)
            quote['volume'] = technical.get('volume', 'N/A')
            plot_html = get_plot_html(df, symbol)
            
            technical_str = ", ".join(f"{k.upper()}: {v}" for k, v in technical.items())
            prompt = f"請根據以下資訊產出中英文雙語股票分析: 股票代號: {symbol}, 目前價格: {quote.get('current_price','N/A')}, 產業分類: {industry_zh} ({industry_en}), 財務指標: {metrics}, 技術指標: {technical_str}. 請提供買入/賣出/持有建議."
            chat_response = openai.ChatCompletion.create(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": "你是一位中英雙語金融分析助理，中英文內容完全對等。請以JSON格式回應: {'recommendation': 'buy' or 'sell' or 'hold', 'rationale': '中文 rationale\\nEnglish rationale', 'risk': '中文 risk\\nEnglish risk', 'summary': '中文 summary\\nEnglish summary'}"},
                    {"role": "user", "content": prompt}
                ],
                max_tokens=999,
                temperature=0.6,
                response_format={"type": "json_object"}
            )
            try:
                gpt_analysis = json.loads(chat_response['choices'][0]['message']['content'])
            except:
                gpt_analysis = chat_response['choices'][0]['message']['content'].strip()
            if isinstance(gpt_analysis, str):
                gpt_analysis = {'summary': gpt_analysis + "\n\n---\n\n*以上分析僅供參考，投資有風險*"}
            
            result = {
                "symbol": symbol,
                "quote": quote,
                "industry_en": industry_en,
                "industry_zh": industry_zh,
                "metrics": metrics,
                "news": news,
                "gpt_analysis": gpt_analysis,
                "plot_html": plot_html,
                "technical": {k: v if v != 'N/A' else 'N/A' for k, v in technical.items()}
            }
        except Exception as e:
            result = {"error": f"資料讀取錯誤: {e}"}
            logger.error(f"Processing error for symbol {symbol}: {e}")

    return render_template("index.html",
                           result=result,
                           symbol_input=symbol_input,
                           QUOTE_FIELDS=QUOTE_FIELDS,
                           METRIC_NAMES_ZH_EN=METRIC_NAMES_ZH_EN,
                           tiers=PRICING_TIERS,
                           stripe_pub_key=STRIPE_PUBLISHABLE_KEY,
                           stripe_mode=STRIPE_MODE,
                           request_count=request_count,
                           current_tier_name=current_tier_name,
                           current_limit=current_limit)

@app.route("/news/<symbol>/<headline>")
def view_news(symbol, headline):
    headline = urllib.parse.unquote(headline)
    news = get_recent_news(symbol)
    selected_news = next((n for n in news if n["headline"] == headline), None)
    if not selected_news:
        flash("❌ 新聞未找到 / News not found", "danger")
        return redirect(url_for("index"))
    return render_template("news_detail.html", news=selected_news, symbol=symbol)

@app.route("/create-checkout-session", methods=["POST"])
def create_checkout_session():
    tier_name = request.form.get("tier")
    tier = next((t for t in PRICING_TIERS if t["name"] == tier_name), None)
    if not tier:
        logger.error(f"Invalid tier requested: {tier_name}")
        return jsonify({"error": "Invalid tier"}), 400
    
    if tier["name"] == "Free":
        session["subscribed"] = False
        session["paid_tier"] = 0
        session["request_count"] = 0
        flash("✅ Switched to Free tier.", "success")
        return jsonify({"url": url_for("index", _external=True)})

    price_id = STRIPE_PRICE_IDS.get(tier_name)
    if not price_id or not validate_price_id(price_id, tier_name):
        logger.error(f"No valid Price ID configured for {tier_name}")
        flash(f"⚠️ Subscription for {tier_name} is currently unavailable.", "warning")
        return jsonify({"error": f"Subscription for {tier_name} is currently unavailable"}), 400

    try:
        logger.info(f"Creating Stripe checkout session for {tier_name} with Price ID: {price_id}")
        session_stripe = stripe.checkout.Session.create(
            payment_method_types=['card'],
            line_items=[{'price': price_id, 'quantity': 1}],
            mode='subscription',
            success_url=url_for('payment_success', tier_name=tier_name, _external=True),
            cancel_url=url_for('index', _external=True)
        )
        return jsonify({"url": session_stripe.url})
    except Exception as e:
        logger.error(f"Unexpected Stripe error: {e}")
        return jsonify({"error": f"Unexpected error: {str(e)}"}), 500

@app.route("/payment-success/<tier_name>")
def payment_success(tier_name):
    tier_index = next((i for i, t in enumerate(PRICING_TIERS) if t["name"] == tier_name), None)
    if tier_index is not None and tier_name != "Free":
        session["subscribed"] = True
        session["paid_tier"] = tier_index
        session["request_count"] = 0
        flash(f"✅ Subscription successful for {tier_name} plan.", "success")
        logger.info(f"Subscription successful for {tier_name} (tier index: {tier_index})")
    return redirect(url_for("index"))

@app.route("/reset", methods=["POST"])
def reset():
    password = request.form.get("password")
    if password == "888888":
        session["request_count"] = 0
        session["subscribed"] = False
        session["paid_tier"] = 0
        session["symbol_cache"] = {}
        flash("✅ Counts reset.", "success")
        logger.info("Session counts reset successfully")
    else:
        flash("❌ Incorrect password.", "danger")
        logger.warning("Failed reset attempt with incorrect password")
    return redirect(url_for("index"))

# ------------------ Run App ------------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, debug=True)
