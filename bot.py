import os
import json
import asyncio
import logging
from datetime import datetime
import aiohttp
from telegram import Update, Bot
from telegram.ext import (
    Application, CommandHandler, ContextTypes, MessageHandler, filters
)

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s')
logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
TOKEN      = os.environ["TELEGRAM_TOKEN"]
CHAT_ID    = os.environ["CHAT_ID"]          # il tuo chat ID
DATA_FILE  = "data.json"

TARGET_PCT = 4.5
STOP_PCT   = 3.0

COINS = [
    ("BTCUSDT",  "Bitcoin",    "BTC"),
    ("ETHUSDT",  "Ethereum",   "ETH"),
    ("BNBUSDT",  "BNB",        "BNB"),
    ("SOLUSDT",  "Solana",     "SOL"),
    ("XRPUSDT",  "Ripple",     "XRP"),
    ("ADAUSDT",  "Cardano",    "ADA"),
    ("DOGEUSDT", "Dogecoin",   "DOGE"),
    ("AVAXUSDT", "Avalanche",  "AVAX"),
    ("LINKUSDT", "Chainlink",  "LINK"),
    ("DOTUSDT",  "Polkadot",   "DOT"),
    ("LTCUSDT",  "Litecoin",   "LTC"),
    ("MATICUSDT","Polygon",    "MATIC"),
]

# ── Persistenza ───────────────────────────────────────────────────────────────
def load_data():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE) as f:
            return json.load(f)
    return {"positions": [], "closed": [], "pnl_total": 0.0, "budget": 400.0}

def save_data(data):
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=2)

# ── Binance helpers ───────────────────────────────────────────────────────────
async def fetch_price(symbol: str) -> float | None:
    url = f"https://api.binance.com/api/v3/ticker/price?symbol={symbol}"
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
                text = await r.text()
                if r.status == 200:
                    d = await r.json(content_type=None)
                    return float(d["price"])
                else:
                    logger.error(f"Binance error {r.status} per {symbol}: {text}")
    except Exception as e:
        logger.error(f"fetch_price {symbol}: {e}")
    return None

async def fetch_klines(symbol: str, interval="1h", limit=50):
    url = f"https://api.binance.com/api/v3/klines?symbol={symbol}&interval={interval}&limit={limit}"
    async with aiohttp.ClientSession() as s:
        async with s.get(url, timeout=aiohttp.ClientTimeout(total=15)) as r:
            if r.status == 200:
                return await r.json()
    return None

async def fetch_ticker_24h(symbol: str) -> dict | None:
    url = f"https://api.binance.com/api/v3/ticker/24hr?symbol={symbol}"
    async with aiohttp.ClientSession() as s:
        async with s.get(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
            if r.status == 200:
                return await r.json()
    return None

# ── Analisi tecnica ───────────────────────────────────────────────────────────
def calc_rsi(closes, period=14):
    if len(closes) < period + 1:
        return None
    gains, losses = 0.0, 0.0
    for i in range(len(closes) - period, len(closes)):
        diff = closes[i] - closes[i-1]
        if diff > 0: gains  += diff
        else:        losses += abs(diff)
    avg_gain = gains  / period
    avg_loss = losses / period
    if avg_loss == 0: return 100.0
    rs = avg_gain / avg_loss
    return 100 - 100 / (1 + rs)

def calc_ema(closes, period):
    k = 2 / (period + 1)
    ema = closes[0]
    for c in closes[1:]:
        ema = c * k + ema * (1 - k)
    return ema

def calc_macd(closes):
    if len(closes) < 26:
        return 0, 0, 0
    ema12 = calc_ema(closes, 12)
    ema26 = calc_ema(closes, 26)
    macd  = ema12 - ema26
    signal_vals = [
        calc_ema(closes[:len(closes)-9+i+1], 12) - calc_ema(closes[:len(closes)-9+i+1], 26)
        for i in range(9)
    ]
    signal = calc_ema(signal_vals, 9)
    return macd, signal, macd - signal

def calc_bb(closes, period=20):
    if len(closes) < period:
        return None
    sl   = closes[-period:]
    mean = sum(sl) / period
    std  = (sum((x - mean)**2 for x in sl) / period) ** 0.5
    return mean + 2*std, mean, mean - 2*std

def analyze(closes, price, change24h):
    rsi  = calc_rsi(closes)
    _, _, hist = calc_macd(closes)
    bb   = calc_bb(closes)
    score = 0
    notes = []

    if rsi is not None:
        if   rsi < 30: score += 3;   notes.append(f"RSI {rsi:.0f} — ipervenduto 📉")
        elif rsi < 45: score += 1.5; notes.append(f"RSI {rsi:.0f} — zona bassa")
        elif rsi > 70: score -= 3;   notes.append(f"RSI {rsi:.0f} — ipercomprato ⚠️")
        elif rsi > 60: score -= 1

    if hist > 0:   score += 2; notes.append("MACD positivo ↑")
    elif hist < 0: score -= 2; notes.append("MACD negativo ↓")

    if bb:
        upper, middle, lower = bb
        if   price <= lower:  score += 2.5; notes.append("Prezzo su BB inferiore 📊")
        elif price >= upper:  score -= 2.5; notes.append("Prezzo su BB superiore 📊")
        elif price < middle:  score += 0.5

    if change24h < -5:  score += 1.5; notes.append(f"Calo {change24h:.1f}% — possibile rimbalzo")
    elif change24h > 8: score -= 1.5; notes.append(f"Rialzo forte +{change24h:.1f}%")

    if   score >= 3:    sig = "BUY";   strength = min(100, int(score/8*100))
    elif score <= -2.5: sig = "SELL";  strength = min(100, int(abs(score)/8*100))
    else:               sig = "WATCH"; strength = min(100, int(abs(score)/8*100)+30)

    return sig, strength, rsi, notes

# ── Formattazione prezzi ──────────────────────────────────────────────────────
def fmt_price(p):
    if p >= 1000: return f"${p:,.2f}"
    if p >= 1:    return f"${p:.4f}"
    return f"${p:.6f}"

def fmt_eur(n):
    sign = "+" if n >= 0 else ""
    return f"{sign}€{abs(n):.2f}"

# ── Scansione e segnali ───────────────────────────────────────────────────────
async def scan_and_notify(bot: Bot):
    logger.info("Scansione mercato...")
    strong_buys = []

    for symbol, name, short in COINS:
        try:
            klines = await fetch_klines(symbol)
            ticker = await fetch_ticker_24h(symbol)
            if not klines or not ticker:
                continue

            closes    = [float(k[4]) for k in klines]
            price     = float(ticker["lastPrice"])
            change24h = float(ticker["priceChangePercent"])

            sig, strength, rsi, notes = analyze(closes, price, change24h)

            if sig == "BUY" and strength >= 55:
                strong_buys.append({
                    "symbol": symbol, "name": name, "short": short,
                    "price": price, "change24h": change24h,
                    "strength": strength, "rsi": rsi, "notes": notes,
                    "sig": sig
                })
        except Exception as e:
            logger.warning(f"{symbol}: {e}")
        await asyncio.sleep(0.5)  # rate limit

    if strong_buys:
        strong_buys.sort(key=lambda x: -x["strength"])
        txt = "📡 *SEGNALI BUY RILEVATI*\n\n"
        for c in strong_buys[:3]:  # max 3 per non spammare
            rsi_str = f"{c['rsi']:.0f}" if c['rsi'] else "—"
            txt += (
                f"🟢 *{c['name']}* ({c['short']})\n"
                f"   Prezzo: {fmt_price(c['price'])}\n"
                f"   24h: {c['change24h']:+.2f}%  |  RSI: {rsi_str}  |  Forza: {c['strength']}%\n"
                f"   {chr(10).join('   • '+n for n in c['notes'][:2])}\n\n"
                f"👉 `/compra {c['short']} <importo €>` per aprire\n\n"
            )
        await bot.send_message(chat_id=CHAT_ID, text=txt, parse_mode="Markdown")
        logger.info(f"Inviati {len(strong_buys)} segnali BUY")
    else:
        logger.info("Nessun segnale forte — skip notifica")

# ── Monitor posizioni aperte ──────────────────────────────────────────────────
async def monitor_positions(bot: Bot):
    data = load_data()
    if not data["positions"]:
        return

    to_close = []
    for pos in data["positions"]:
        price = await fetch_price(pos["symbol"])
        if price is None:
            continue
        pos["current_price"] = price
        pct = (price - pos["entry_price"]) / pos["entry_price"] * 100
        pnl = pos["amount"] * pct / 100

        if pct >= TARGET_PCT:
            msg = (
                f"🎯 *TARGET RAGGIUNTO!*\n\n"
                f"*{pos['name']}* ({pos['short']})\n"
                f"Entry: {fmt_price(pos['entry_price'])}\n"
                f"Attuale: {fmt_price(price)}\n"
                f"📈 +{pct:.2f}% → {fmt_eur(pnl)}\n\n"
                f"💰 Hai guadagnato *{fmt_eur(pnl)}* su €{pos['amount']:.2f} investiti!\n\n"
                f"👉 `/vendi {pos['short']}` per chiudere\n"
                f"_(o aspetta per guadagnare di più — a tuo rischio)_"
            )
            await bot.send_message(chat_id=CHAT_ID, text=msg, parse_mode="Markdown")
            to_close.append((pos["id"], price, "target"))

        elif pct <= -STOP_PCT:
            msg = (
                f"🛑 *STOP LOSS ATTIVATO*\n\n"
                f"*{pos['name']}* ({pos['short']})\n"
                f"Entry: {fmt_price(pos['entry_price'])}\n"
                f"Attuale: {fmt_price(price)}\n"
                f"📉 {pct:.2f}% → {fmt_eur(pnl)}\n\n"
                f"⚠️ Stop loss a -{STOP_PCT}% scattato per proteggere il capitale.\n\n"
                f"👉 `/vendi {pos['short']}` per chiudere la posizione"
            )
            await bot.send_message(chat_id=CHAT_ID, text=msg, parse_mode="Markdown")
            to_close.append((pos["id"], price, "stop"))

        await asyncio.sleep(0.3)

    # Chiudi le posizioni che hanno raggiunto target/stop
    for pid, close_price, reason in to_close:
        _close_position(data, pid, close_price, reason)

    save_data(data)

def _close_position(data, pos_id, close_price, reason):
    idx = next((i for i, p in enumerate(data["positions"]) if p["id"] == pos_id), None)
    if idx is None:
        return
    pos = data["positions"].pop(idx)
    pct = (close_price - pos["entry_price"]) / pos["entry_price"] * 100
    pnl = pos["amount"] * pct / 100
    data["pnl_total"] = round(data["pnl_total"] + pnl, 4)
    data["closed"].insert(0, {
        **pos,
        "close_price": close_price,
        "close_reason": reason,
        "pnl": round(pnl, 4),
        "pct": round(pct, 4),
        "closed_at": datetime.now().strftime("%d/%m %H:%M")
    })

# ── Comandi Telegram ──────────────────────────────────────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = (
        "👋 *CryptoAdvisor Bot attivo!*\n\n"
        "Riceverai notifiche automatiche ogni 30 minuti.\n\n"
        "*Comandi disponibili:*\n"
        "📊 `/portafoglio` — vedi posizioni e P&L\n"
        "💰 `/compra BTC 50` — apri posizione (€50 su BTC)\n"
        "🔴 `/vendi BTC` — chiudi posizione su BTC\n"
        "📡 `/segnali` — analisi immediata del mercato\n"
        "⚙️ `/budget 400` — imposta budget totale\n"
        "📋 `/storico` — ultime operazioni chiuse\n"
        "❓ `/aiuto` — mostra questo messaggio"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")

async def cmd_segnali(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔍 Analizzo il mercato... (30-60 secondi)")
    await scan_and_notify(ctx.bot)

async def cmd_compra(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    args = ctx.args
    if len(args) < 2:
        await update.message.reply_text("❌ Uso: `/compra BTC 50`", parse_mode="Markdown")
        return

    short  = args[0].upper()
    try:    amount = float(args[1])
    except: await update.message.reply_text("❌ Importo non valido"); return

    symbol = next((s for s, n, sh in COINS if sh == short), None)
    name   = next((n for s, n, sh in COINS if sh == short), None)
    if not symbol:
        await update.message.reply_text(f"❌ Crypto '{short}' non trovata")
        return

    data = load_data()
    invested = sum(p["amount"] for p in data["positions"])
    available = data["budget"] - invested
    if amount > available:
        await update.message.reply_text(
            f"⚠️ Budget insufficiente.\nDisponibile: €{available:.2f}\nRichiesto: €{amount:.2f}"
        )
        return

    price = await fetch_price(symbol)
    if price is None:
        await update.message.reply_text("❌ Errore nel recuperare il prezzo. Riprova.")
        return

    pos = {
        "id":          len(data["positions"]) + len(data["closed"]) + 1,
        "symbol":      symbol,
        "name":        name,
        "short":       short,
        "entry_price": price,
        "current_price": price,
        "amount":      amount,
        "target_price": round(price * (1 + TARGET_PCT/100), 8),
        "stop_price":  round(price * (1 - STOP_PCT/100), 8),
        "opened_at":   datetime.now().strftime("%d/%m %H:%M"),
    }
    data["positions"].append(pos)
    save_data(data)

    target_gain = amount * TARGET_PCT / 100
    max_loss    = amount * STOP_PCT / 100
    msg = (
        f"✅ *Posizione aperta!*\n\n"
        f"*{name}* ({short})\n"
        f"💵 Entry: {fmt_price(price)}\n"
        f"💶 Investito: €{amount:.2f}\n\n"
        f"🎯 Target: {fmt_price(pos['target_price'])} (+{TARGET_PCT}%) → *+€{target_gain:.2f}*\n"
        f"🛑 Stop: {fmt_price(pos['stop_price'])} (-{STOP_PCT}%) → *-€{max_loss:.2f}*\n\n"
        f"_Monitoraggio attivo ogni 30 minuti._"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")

async def cmd_vendi(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    args = ctx.args
    if not args:
        await update.message.reply_text("❌ Uso: `/vendi BTC`", parse_mode="Markdown")
        return

    short = args[0].upper()
    data  = load_data()
    pos   = next((p for p in data["positions"] if p["short"] == short), None)
    if not pos:
        await update.message.reply_text(f"❌ Nessuna posizione aperta su {short}")
        return

    price = await fetch_price(pos["symbol"])
    if price is None:
        await update.message.reply_text("❌ Errore prezzo. Riprova.")
        return

    _close_position(data, pos["id"], price, "manual")
    save_data(data)

    pct = (price - pos["entry_price"]) / pos["entry_price"] * 100
    pnl = pos["amount"] * pct / 100
    emoji = "📈" if pnl >= 0 else "📉"
    msg = (
        f"🔴 *Posizione chiusa* — {pos['name']}\n\n"
        f"Entry: {fmt_price(pos['entry_price'])}\n"
        f"Exit:  {fmt_price(price)}\n"
        f"{emoji} {pct:+.2f}% → *{fmt_eur(pnl)}*\n\n"
        f"P&L totale: *{fmt_eur(data['pnl_total'])}*"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")

async def cmd_portafoglio(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    data     = load_data()
    invested = sum(p["amount"] for p in data["positions"])
    available = data["budget"] - invested

    lines = [
        f"📊 *PORTAFOGLIO*\n",
        f"💰 Budget: €{data['budget']:.2f}",
        f"📌 Investito: €{invested:.2f}",
        f"✅ Disponibile: €{available:.2f}",
        f"📈 P&L realizzato: *{fmt_eur(data['pnl_total'])}*\n",
    ]

    if data["positions"]:
        lines.append("*Posizioni aperte:*")
        for p in data["positions"]:
            price = await fetch_price(p["symbol"])
            if price:
                pct = (price - p["entry_price"]) / p["entry_price"] * 100
                pnl = p["amount"] * pct / 100
                emoji = "🟢" if pnl >= 0 else "🔴"
                lines.append(
                    f"{emoji} *{p['short']}* — €{p['amount']:.0f} → "
                    f"{pct:+.2f}% ({fmt_eur(pnl)})\n"
                    f"   Entry {fmt_price(p['entry_price'])} | Ora {fmt_price(price)}\n"
                    f"   Target {fmt_price(p['target_price'])} | Stop {fmt_price(p['stop_price'])}"
                )
            await asyncio.sleep(0.2)
    else:
        lines.append("_Nessuna posizione aperta._")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

async def cmd_storico(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    data = load_data()
    if not data["closed"]:
        await update.message.reply_text("📋 Nessuna operazione chiusa.")
        return

    lines = ["📋 *ULTIME OPERAZIONI*\n"]
    for op in data["closed"][:10]:
        emoji = "✅" if op["pnl"] >= 0 else "❌"
        reason_icon = {"target": "🎯", "stop": "🛑", "manual": "👤"}.get(op.get("close_reason",""), "")
        lines.append(
            f"{emoji} {reason_icon} *{op['short']}* — {op.get('closed_at','')}\n"
            f"   {op['pct']:+.2f}% → *{fmt_eur(op['pnl'])}*\n"
            f"   Entry {fmt_price(op['entry_price'])} → Exit {fmt_price(op['close_price'])}"
        )
    lines.append(f"\n💰 P&L totale: *{fmt_eur(data['pnl_total'])}*")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

async def cmd_budget(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("❌ Uso: `/budget 400`", parse_mode="Markdown")
        return
    try:
        b = float(ctx.args[0])
    except:
        await update.message.reply_text("❌ Valore non valido")
        return
    data = load_data()
    data["budget"] = b
    save_data(data)
    await update.message.reply_text(f"✅ Budget impostato a €{b:.2f}")

async def cmd_aiuto(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await cmd_start(update, ctx)

# ── Job periodici ─────────────────────────────────────────────────────────────
async def job_scan(ctx: ContextTypes.DEFAULT_TYPE):
    await scan_and_notify(ctx.bot)

async def job_monitor(ctx: ContextTypes.DEFAULT_TYPE):
    await monitor_positions(ctx.bot)

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start",       cmd_start))
    app.add_handler(CommandHandler("segnali",     cmd_segnali))
    app.add_handler(CommandHandler("compra",      cmd_compra))
    app.add_handler(CommandHandler("vendi",       cmd_vendi))
    app.add_handler(CommandHandler("portafoglio", cmd_portafoglio))
    app.add_handler(CommandHandler("storico",     cmd_storico))
    app.add_handler(CommandHandler("budget",      cmd_budget))
    app.add_handler(CommandHandler("aiuto",       cmd_aiuto))

    jq = app.job_queue
    jq.run_repeating(job_scan,    interval=30*60, first=60)   # segnali ogni 30 min
    jq.run_repeating(job_monitor, interval=5*60,  first=30)   # monitor ogni 5 min

    logger.info("Bot avviato ✅")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
