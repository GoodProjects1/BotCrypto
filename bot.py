import os
import json
import asyncio
import logging
from datetime import datetime
import aiohttp
from telegram import Update, Bot
from telegram.ext import (
    Application, CommandHandler, ContextTypes
)

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s')
logger = logging.getLogger(__name__)

TOKEN     = os.environ["TELEGRAM_TOKEN"]
CHAT_ID   = os.environ["CHAT_ID"]
DATA_FILE = "data.json"

TARGET_PCT = 4.5
STOP_PCT   = 3.0

# CoinGecko IDs → (nome, short)
COINS = [
    ("bitcoin",       "Bitcoin",    "BTC"),
    ("ethereum",      "Ethereum",   "ETH"),
    ("binancecoin",   "BNB",        "BNB"),
    ("solana",        "Solana",     "SOL"),
    ("ripple",        "Ripple",     "XRP"),
    ("cardano",       "Cardano",    "ADA"),
    ("dogecoin",      "Dogecoin",   "DOGE"),
    ("avalanche-2",   "Avalanche",  "AVAX"),
    ("chainlink",     "Chainlink",  "LINK"),
    ("polkadot",      "Polkadot",   "DOT"),
    ("litecoin",      "Litecoin",   "LTC"),
    ("matic-network", "Polygon",    "MATIC"),
]

COINGECKO_IDS = [c[0] for c in COINS]
SHORT_TO_ID   = {c[2]: c[0] for c in COINS}
SHORT_TO_NAME = {c[2]: c[1] for c in COINS}

# ── Persistenza ───────────────────────────────────────────────────────────────
def load_data():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE) as f:
            return json.load(f)
    return {"positions": [], "closed": [], "pnl_total": 0.0, "budget": 400.0}

def save_data(data):
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=2)

# ── CoinGecko helpers ─────────────────────────────────────────────────────────
HEADERS = {"accept": "application/json"}

async def fetch_prices_all() -> dict:
    """Ritorna {coingecko_id: price_eur}"""
    ids = ",".join(COINGECKO_IDS)
    url = f"https://api.coingecko.com/api/v3/simple/price?ids={ids}&vs_currencies=usd&include_24hr_change=true"
    try:
        async with aiohttp.ClientSession(headers=HEADERS) as s:
            async with s.get(url, timeout=aiohttp.ClientTimeout(total=15)) as r:
                if r.status == 200:
                    return await r.json()
                logger.error(f"CoinGecko prices error {r.status}")
    except Exception as e:
        logger.error(f"fetch_prices_all: {e}")
    return {}

async def fetch_price(coin_id: str) -> float | None:
    url = f"https://api.coingecko.com/api/v3/simple/price?ids={coin_id}&vs_currencies=usd"
    try:
        async with aiohttp.ClientSession(headers=HEADERS) as s:
            async with s.get(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
                if r.status == 200:
                    d = await r.json()
                    return d[coin_id]["usd"]
                logger.error(f"CoinGecko single price error {r.status} for {coin_id}")
    except Exception as e:
        logger.error(f"fetch_price {coin_id}: {e}")
    return None

async def fetch_ohlc(coin_id: str, days=2) -> list | None:
    """OHLC orario da CoinGecko — restituisce lista di close prices"""
    url = f"https://api.coingecko.com/api/v3/coins/{coin_id}/ohlc?vs_currency=usd&days={days}"
    try:
        async with aiohttp.ClientSession(headers=HEADERS) as s:
            async with s.get(url, timeout=aiohttp.ClientTimeout(total=15)) as r:
                if r.status == 200:
                    data = await r.json()
                    # ogni entry: [timestamp, open, high, low, close]
                    return [row[4] for row in data]
    except Exception as e:
        logger.error(f"fetch_ohlc {coin_id}: {e}")
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
    avg_gain = gains / period
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

def calc_macd_hist(closes):
    if len(closes) < 26:
        return 0
    ema12 = calc_ema(closes, 12)
    ema26 = calc_ema(closes, 26)
    macd  = ema12 - ema26
    if len(closes) >= 35:
        signal_vals = [
            calc_ema(closes[:len(closes)-9+i+1], 12) - calc_ema(closes[:len(closes)-9+i+1], 26)
            for i in range(9)
        ]
        signal = calc_ema(signal_vals, 9)
        return macd - signal
    return macd

def calc_bb(closes, period=20):
    if len(closes) < period:
        return None
    sl   = closes[-period:]
    mean = sum(sl) / period
    std  = (sum((x - mean)**2 for x in sl) / period) ** 0.5
    return mean + 2*std, mean, mean - 2*std

def analyze(closes, price, change24h):
    rsi  = calc_rsi(closes)
    hist = calc_macd_hist(closes)
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

# ── Formattazione ─────────────────────────────────────────────────────────────
def fmt_price(p):
    if p is None: return "—"
    if p >= 1000: return f"${p:,.2f}"
    if p >= 1:    return f"${p:.4f}"
    return f"${p:.6f}"

def fmt_eur(n):
    sign = "+" if n >= 0 else ""
    return f"{sign}€{abs(n):.2f}"

# ── Scansione segnali ─────────────────────────────────────────────────────────
async def scan_and_notify(bot: Bot):
    logger.info("Scansione mercato (CoinGecko)...")
    prices_data = await fetch_prices_all()
    if not prices_data:
        logger.warning("Nessun dato prezzi ricevuto")
        return

    strong_buys = []
    for coin_id, name, short in COINS:
        try:
            entry = prices_data.get(coin_id)
            if not entry:
                continue
            price     = entry["usd"]
            change24h = entry.get("usd_24h_change", 0)

            closes = await fetch_ohlc(coin_id, days=2)
            await asyncio.sleep(1.5)  # CoinGecko free tier: max ~30 req/min
            if not closes or len(closes) < 15:
                continue

            sig, strength, rsi, notes = analyze(closes, price, change24h)

            if sig == "BUY" and strength >= 55:
                strong_buys.append({
                    "coin_id": coin_id, "name": name, "short": short,
                    "price": price, "change24h": change24h,
                    "strength": strength, "rsi": rsi, "notes": notes,
                })
        except Exception as e:
            logger.warning(f"{coin_id}: {e}")

    if strong_buys:
        strong_buys.sort(key=lambda x: -x["strength"])
        txt = "📡 *SEGNALI BUY RILEVATI*\n\n"
        for c in strong_buys[:3]:
            rsi_str = f"{c['rsi']:.0f}" if c['rsi'] else "—"
            txt += (
                f"🟢 *{c['name']}* ({c['short']})\n"
                f"   Prezzo: {fmt_price(c['price'])}\n"
                f"   24h: {c['change24h']:+.2f}%  |  RSI: {rsi_str}  |  Forza: {c['strength']}%\n"
                f"   {chr(10).join('   • '+n for n in c['notes'][:2])}\n\n"
                f"👉 `/compra {c['short']} <importo €>` per aprire\n\n"
            )
        await bot.send_message(chat_id=CHAT_ID, text=txt, parse_mode="Markdown")
    else:
        logger.info("Nessun segnale forte — skip notifica")

# ── Monitor posizioni ─────────────────────────────────────────────────────────
async def monitor_positions(bot: Bot):
    data = load_data()
    if not data["positions"]:
        return

    to_close = []
    for pos in data["positions"]:
        price = await fetch_price(pos["coin_id"])
        await asyncio.sleep(1.5)
        if price is None:
            logger.warning(f"Prezzo non disponibile per {pos['short']}")
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
                f"📈 +{pct:.2f}% → *{fmt_eur(pnl)}*\n\n"
                f"💰 Guadagno: *{fmt_eur(pnl)}* su €{pos['amount']:.2f} investiti!\n\n"
                f"👉 `/vendi {pos['short']}` per chiudere\n"
                f"_(o aspetta — a tuo rischio)_"
            )
            await bot.send_message(chat_id=CHAT_ID, text=msg, parse_mode="Markdown")
            to_close.append((pos["id"], price, "target"))

        elif pct <= -STOP_PCT:
            msg = (
                f"🛑 *STOP LOSS ATTIVATO*\n\n"
                f"*{pos['name']}* ({pos['short']})\n"
                f"Entry: {fmt_price(pos['entry_price'])}\n"
                f"Attuale: {fmt_price(price)}\n"
                f"📉 {pct:.2f}% → *{fmt_eur(pnl)}*\n\n"
                f"⚠️ Stop loss a -{STOP_PCT}% per proteggere il capitale.\n\n"
                f"👉 `/vendi {pos['short']}` per chiudere"
            )
            await bot.send_message(chat_id=CHAT_ID, text=msg, parse_mode="Markdown")
            to_close.append((pos["id"], price, "stop"))

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
        "close_price":  close_price,
        "close_reason": reason,
        "pnl":          round(pnl, 4),
        "pct":          round(pct, 4),
        "closed_at":    datetime.now().strftime("%d/%m %H:%M"),
    })

# ── Comandi ───────────────────────────────────────────────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = (
        "👋 *CryptoAdvisor Bot attivo!*\n\n"
        "Riceverai notifiche automatiche ogni 30 minuti.\n\n"
        "*Comandi:*\n"
        "📊 `/portafoglio` — posizioni e P&L live\n"
        "💰 `/compra LINK 100` — apri posizione\n"
        "🔴 `/vendi LINK` — chiudi posizione\n"
        "📡 `/segnali` — analisi immediata\n"
        "⚙️ `/budget 400` — imposta budget\n"
        "📋 `/storico` — operazioni chiuse\n"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")

async def cmd_segnali(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔍 Analizzo il mercato... (60 secondi circa)")
    await scan_and_notify(ctx.bot)

async def cmd_compra(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    args = ctx.args
    if len(args) < 2:
        await update.message.reply_text("❌ Uso: `/compra LINK 100`", parse_mode="Markdown")
        return

    short = args[0].upper()
    try:    amount = float(args[1])
    except: await update.message.reply_text("❌ Importo non valido"); return

    coin_id = SHORT_TO_ID.get(short)
    name    = SHORT_TO_NAME.get(short)
    if not coin_id:
        available = ", ".join(SHORT_TO_ID.keys())
        await update.message.reply_text(f"❌ Crypto '{short}' non trovata.\nDisponibili: {available}")
        return

    data      = load_data()
    invested  = sum(p["amount"] for p in data["positions"])
    available = data["budget"] - invested
    if amount > available:
        await update.message.reply_text(
            f"⚠️ Budget insufficiente.\nDisponibile: €{available:.2f} | Richiesto: €{amount:.2f}"
        )
        return

    await update.message.reply_text(f"⏳ Recupero prezzo {short}...")
    price = await fetch_price(coin_id)
    if price is None:
        await update.message.reply_text("❌ Errore nel recuperare il prezzo. Riprova tra qualche secondo.")
        return

    pos = {
        "id":            len(data["positions"]) + len(data["closed"]) + 1,
        "coin_id":       coin_id,
        "name":          name,
        "short":         short,
        "entry_price":   price,
        "current_price": price,
        "amount":        amount,
        "target_price":  round(price * (1 + TARGET_PCT/100), 8),
        "stop_price":    round(price * (1 - STOP_PCT/100), 8),
        "opened_at":     datetime.now().strftime("%d/%m %H:%M"),
    }
    data["positions"].append(pos)
    save_data(data)

    gain = amount * TARGET_PCT / 100
    loss = amount * STOP_PCT   / 100
    msg = (
        f"✅ *Posizione aperta!*\n\n"
        f"*{name}* ({short})\n"
        f"💵 Entry: {fmt_price(price)}\n"
        f"💶 Investito: €{amount:.2f}\n\n"
        f"🎯 Target: {fmt_price(pos['target_price'])} (+{TARGET_PCT}%) → *+€{gain:.2f}*\n"
        f"🛑 Stop: {fmt_price(pos['stop_price'])} (-{STOP_PCT}%) → *-€{loss:.2f}*\n\n"
        f"_Monitoraggio attivo ogni 5 minuti._"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")

async def cmd_vendi(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("❌ Uso: `/vendi LINK`", parse_mode="Markdown")
        return
    short = ctx.args[0].upper()
    data  = load_data()
    pos   = next((p for p in data["positions"] if p["short"] == short), None)
    if not pos:
        await update.message.reply_text(f"❌ Nessuna posizione aperta su {short}")
        return

    await update.message.reply_text(f"⏳ Recupero prezzo attuale {short}...")
    price = await fetch_price(pos["coin_id"])
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
        f"Entry:  {fmt_price(pos['entry_price'])}\n"
        f"Exit:   {fmt_price(price)}\n"
        f"{emoji} {pct:+.2f}% → *{fmt_eur(pnl)}*\n\n"
        f"P&L totale: *{fmt_eur(data['pnl_total'])}*"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")

async def cmd_portafoglio(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    data     = load_data()
    invested = sum(p["amount"] for p in data["positions"])
    avail    = data["budget"] - invested

    lines = [
        "📊 *PORTAFOGLIO*\n",
        f"💰 Budget: €{data['budget']:.2f}",
        f"📌 Investito: €{invested:.2f}",
        f"✅ Disponibile: €{avail:.2f}",
        f"📈 P&L realizzato: *{fmt_eur(data['pnl_total'])}*\n",
    ]

    if data["positions"]:
        lines.append("*Posizioni aperte:*")
        for p in data["positions"]:
            price = await fetch_price(p["coin_id"])
            await asyncio.sleep(1.5)
            if price:
                pct = (price - p["entry_price"]) / p["entry_price"] * 100
                pnl = p["amount"] * pct / 100
                emoji = "🟢" if pnl >= 0 else "🔴"
                lines.append(
                    f"{emoji} *{p['short']}* — €{p['amount']:.0f} → "
                    f"{pct:+.2f}% (*{fmt_eur(pnl)}*)\n"
                    f"   Entry {fmt_price(p['entry_price'])} | Ora {fmt_price(price)}\n"
                    f"   🎯 {fmt_price(p['target_price'])} | 🛑 {fmt_price(p['stop_price'])}"
                )
            else:
                lines.append(f"⚠️ *{p['short']}* — prezzo non disponibile")
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
        emoji  = "✅" if op["pnl"] >= 0 else "❌"
        reason = {"target": "🎯", "stop": "🛑", "manual": "👤"}.get(op.get("close_reason",""), "")
        lines.append(
            f"{emoji} {reason} *{op['short']}* — {op.get('closed_at','')}\n"
            f"   {op['pct']:+.2f}% → *{fmt_eur(op['pnl'])}*\n"
            f"   {fmt_price(op['entry_price'])} → {fmt_price(op['close_price'])}"
        )
    lines.append(f"\n💰 P&L totale: *{fmt_eur(data['pnl_total'])}*")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

async def cmd_budget(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("❌ Uso: `/budget 400`", parse_mode="Markdown")
        return
    try:    b = float(ctx.args[0])
    except: await update.message.reply_text("❌ Valore non valido"); return
    data = load_data()
    data["budget"] = b
    save_data(data)
    await update.message.reply_text(f"✅ Budget impostato a €{b:.2f}")

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
    app.add_handler(CommandHandler("aiuto",       cmd_start))

    jq = app.job_queue
    jq.run_repeating(job_scan,    interval=30*60, first=60)
    jq.run_repeating(job_monitor, interval=5*60,  first=30)

    logger.info("Bot avviato con CoinGecko ✅")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
