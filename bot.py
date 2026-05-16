import os
import json
import asyncio
import logging
from datetime import datetime
import aiohttp
from telegram import Update, Bot
from telegram.ext import Application, CommandHandler, ContextTypes

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s')
logger = logging.getLogger(__name__)

TOKEN     = os.environ["TELEGRAM_TOKEN"]
CHAT_ID   = os.environ["CHAT_ID"]
DATA_FILE = "data.json"

TARGET_PCT = 4.5
STOP_PCT   = 3.0

COINS = [
    ("bitcoin",       "Bitcoin",   "BTC"),
    ("ethereum",      "Ethereum",  "ETH"),
    ("binancecoin",   "BNB",       "BNB"),
    ("solana",        "Solana",    "SOL"),
    ("ripple",        "Ripple",    "XRP"),
    ("cardano",       "Cardano",   "ADA"),
    ("dogecoin",      "Dogecoin",  "DOGE"),
    ("avalanche-2",   "Avalanche", "AVAX"),
    ("chainlink",     "Chainlink", "LINK"),
    ("polkadot",      "Polkadot",  "DOT"),
    ("litecoin",      "Litecoin",  "LTC"),
    ("matic-network", "Polygon",   "MATIC"),
]

SHORT_TO_ID   = {c[2]: c[0] for c in COINS}
SHORT_TO_NAME = {c[2]: c[1] for c in COINS}
ALL_IDS       = ",".join(c[0] for c in COINS)

# ── Persistenza ───────────────────────────────────────────────────────────────
def load_data():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE) as f:
            return json.load(f)
    return {"positions": [], "closed": [], "pnl_total": 0.0, "budget": 400.0}

def save_data(data):
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=2)

# ── CoinGecko ─────────────────────────────────────────────────────────────────
async def fetch_all_prices() -> dict:
    url = (f"https://api.coingecko.com/api/v3/simple/price"
           f"?ids={ALL_IDS}&vs_currencies=usd&include_24hr_change=true")
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(url, timeout=aiohttp.ClientTimeout(total=20)) as r:
                if r.status == 200:
                    return await r.json()
                logger.error(f"fetch_all_prices: HTTP {r.status}")
    except Exception as e:
        logger.error(f"fetch_all_prices: {e}")
    return {}

async def fetch_single_price(coin_id: str) -> float | None:
    url = f"https://api.coingecko.com/api/v3/simple/price?ids={coin_id}&vs_currencies=usd"
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
                if r.status == 200:
                    d = await r.json()
                    return d[coin_id]["usd"]
                logger.error(f"fetch_single_price {coin_id}: HTTP {r.status}")
    except Exception as e:
        logger.error(f"fetch_single_price {coin_id}: {e}")
    return None

async def fetch_ohlc(coin_id: str, days: int = 1) -> list | None:
    url = f"https://api.coingecko.com/api/v3/coins/{coin_id}/ohlc?vs_currency=usd&days={days}"
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(url, timeout=aiohttp.ClientTimeout(total=15)) as r:
                if r.status == 200:
                    rows = await r.json()
                    return [row[4] for row in rows]
                logger.error(f"fetch_ohlc {coin_id}: HTTP {r.status}")
    except Exception as e:
        logger.error(f"fetch_ohlc {coin_id}: {e}")
    return None

# ── Analisi tecnica ───────────────────────────────────────────────────────────
def calc_rsi(closes, period=14):
    if len(closes) < period + 1:
        return None
    gains = losses = 0.0
    for i in range(len(closes) - period, len(closes)):
        d = closes[i] - closes[i-1]
        if d > 0: gains  += d
        else:     losses += abs(d)
    ag, al = gains / period, losses / period
    return 100.0 if al == 0 else 100 - 100 / (1 + ag / al)

def calc_ema(data, period):
    k, ema = 2 / (period + 1), data[0]
    for v in data[1:]:
        ema = v * k + ema * (1 - k)
    return ema

def calc_macd_hist(closes):
    if len(closes) < 26: return 0
    macd = calc_ema(closes, 12) - calc_ema(closes, 26)
    if len(closes) >= 35:
        sv = [calc_ema(closes[:len(closes)-9+i+1], 12) -
              calc_ema(closes[:len(closes)-9+i+1], 26) for i in range(9)]
        return macd - calc_ema(sv, 9)
    return macd

def calc_bb(closes, period=20):
    if len(closes) < period: return None
    sl = closes[-period:]
    m  = sum(sl) / period
    s  = (sum((x-m)**2 for x in sl) / period) ** 0.5
    return m + 2*s, m, m - 2*s

def analyze(closes, price, change24h):
    rsi   = calc_rsi(closes)
    hist  = calc_macd_hist(closes)
    bb    = calc_bb(closes)
    score = 0
    notes = []

    if rsi is not None:
        if   rsi < 30: score += 3;   notes.append(f"RSI {rsi:.0f} ipervenduto 📉")
        elif rsi < 45: score += 1.5; notes.append(f"RSI {rsi:.0f} zona bassa")
        elif rsi > 70: score -= 3;   notes.append(f"RSI {rsi:.0f} ipercomprato ⚠️")
        elif rsi > 60: score -= 1

    if hist > 0:   score += 2; notes.append("MACD positivo ↑")
    elif hist < 0: score -= 2; notes.append("MACD negativo ↓")

    if bb:
        upper, middle, lower = bb
        if   price <= lower:  score += 2.5; notes.append("Su BB inferiore 📊")
        elif price >= upper:  score -= 2.5; notes.append("Su BB superiore 📊")
        elif price < middle:  score += 0.5

    if   change24h < -5:  score += 1.5; notes.append(f"Calo {change24h:.1f}% 24h")
    elif change24h >  8:  score -= 1.5; notes.append(f"Rialzo forte +{change24h:.1f}%")

    if   score >= 3:    sig = "BUY";   st = min(100, int(score/8*100))
    elif score <= -2.5: sig = "SELL";  st = min(100, int(abs(score)/8*100))
    else:               sig = "WATCH"; st = min(100, int(abs(score)/8*100)+30)

    return sig, st, rsi, notes

# ── Formattazione ─────────────────────────────────────────────────────────────
def fp(p):
    if p is None: return "—"
    if p >= 1000: return f"${p:,.2f}"
    if p >= 1:    return f"${p:.4f}"
    return f"${p:.6f}"

def fe(n):
    return f"{'+'if n>=0 else''}€{abs(n):.2f}"

# ── SCAN — notifica SOLO se BUY forte ────────────────────────────────────────
async def scan_and_notify(bot: Bot, notify_empty: bool = False):
    """
    notify_empty=False → silenzioso se nessun BUY forte (job automatico)
    notify_empty=True  → manda sempre un messaggio (comando /segnali manuale)
    """
    logger.info("Scansione mercato...")
    prices = await fetch_all_prices()
    if not prices:
        logger.warning("Nessun dato prezzi")
        return

    candidates = []
    for coin_id, name, short in COINS:
        entry = prices.get(coin_id)
        if not entry: continue
        candidates.append((coin_id, name, short,
                           entry["usd"], entry.get("usd_24h_change", 0) or 0))
    candidates.sort(key=lambda x: x[4])   # dal più in calo

    strong_buys = []
    for coin_id, name, short, price, change24h in candidates[:5]:
        closes = await fetch_ohlc(coin_id, days=1)
        await asyncio.sleep(2)
        if not closes or len(closes) < 10:
            logger.warning(f"OHLC insufficiente {short}")
            continue
        sig, strength, rsi, notes = analyze(closes, price, change24h)
        logger.info(f"{short}: {sig} str={strength} rsi={f'{rsi:.1f}' if rsi else 'N/A'}")
        if sig == "BUY" and strength >= 45:
            strong_buys.append({"name": name, "short": short, "price": price,
                                 "change24h": change24h, "strength": strength,
                                 "rsi": rsi, "notes": notes})

    if not strong_buys:
        # Job automatico → silenzio totale
        # Comando manuale → mostra riepilogo prezzi
        if notify_empty:
            txt = "📡 *ANALISI MERCATO*\n\nNessun segnale BUY forte al momento.\n\n*Prezzi aggiornati:*\n"
            for coin_id, name, short, price, change24h in candidates[:6]:
                txt += f"{'🟢'if change24h>=0 else'🔴'} *{short}*: {fp(price)} ({change24h:+.2f}%)\n"
            txt += "\n_Il bot ti avviserà automaticamente quando trova segnali forti._"
            await bot.send_message(chat_id=CHAT_ID, text=txt, parse_mode="Markdown")
        else:
            logger.info("Nessun segnale forte — nessuna notifica inviata")
        return

    strong_buys.sort(key=lambda x: -x["strength"])
    txt = "🚨 *SEGNALE BUY FORTE RILEVATO*\n\n"
    for c in strong_buys[:3]:
        rsi_s = f"{c['rsi']:.0f}" if c['rsi'] else "—"
        txt += (
            f"🟢 *{c['name']}* ({c['short']})\n"
            f"   {fp(c['price'])}  |  24h: {c['change24h']:+.2f}%  |  RSI: {rsi_s}  |  Forza: {c['strength']}%\n"
            f"   {chr(10).join('   • '+n for n in c['notes'][:2])}\n\n"
            f"👉 `/compra {c['short']} <importo>` per aprire\n\n"
        )
    await bot.send_message(chat_id=CHAT_ID, text=txt, parse_mode="Markdown")

# ── Monitor posizioni — SOLO notifica, NON chiude ────────────────────────────
async def monitor_positions(bot: Bot):
    """
    Controlla target e stop loss.
    Manda notifica ma NON chiude la posizione — decide l'utente con /vendi.
    Evita di rispammare la stessa notifica usando il flag 'alerted'.
    """
    data = load_data()
    if not data["positions"]: return

    ids = ",".join({p["coin_id"] for p in data["positions"]})
    prices = {}
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(
                f"https://api.coingecko.com/api/v3/simple/price?ids={ids}&vs_currencies=usd",
                timeout=aiohttp.ClientTimeout(total=10)
            ) as r:
                if r.status == 200:
                    d = await r.json()
                    prices = {k: v["usd"] for k, v in d.items()}
    except Exception as e:
        logger.error(f"monitor fetch: {e}"); return

    changed = False
    for pos in data["positions"]:
        price = prices.get(pos["coin_id"])
        if price is None: continue

        pos["current_price"] = price
        changed = True
        pct = (price - pos["entry_price"]) / pos["entry_price"] * 100
        pnl = pos["amount"] * pct / 100

        # Target raggiunto
        if pct >= TARGET_PCT and not pos.get("target_alerted"):
            await bot.send_message(chat_id=CHAT_ID, parse_mode="Markdown", text=(
                f"🎯 *TARGET RAGGIUNTO!*\n\n"
                f"*{pos['name']}* ({pos['short']})\n"
                f"Entry: {fp(pos['entry_price'])} → Ora: {fp(price)}\n"
                f"📈 *+{pct:.2f}%* → *{fe(pnl)}*\n\n"
                f"💡 La posizione rimane aperta finché non vendi tu.\n"
                f"👉 `/vendi {pos['short']}` per incassare"
            ))
            pos["target_alerted"] = True   # non rispammare
            pos["stop_alerted"]   = False  # reset stop nel caso il prezzo risalga

        # Stop loss
        elif pct <= -STOP_PCT and not pos.get("stop_alerted"):
            await bot.send_message(chat_id=CHAT_ID, parse_mode="Markdown", text=(
                f"🛑 *STOP LOSS*\n\n"
                f"*{pos['name']}* ({pos['short']})\n"
                f"Entry: {fp(pos['entry_price'])} → Ora: {fp(price)}\n"
                f"📉 *{pct:.2f}%* → *{fe(pnl)}*\n\n"
                f"💡 La posizione rimane aperta finché non vendi tu.\n"
                f"👉 `/vendi {pos['short']}` per limitare la perdita"
            ))
            pos["stop_alerted"]   = True   # non rispammare
            pos["target_alerted"] = False  # reset target

        # Reset alert se il prezzo è tornato in zona neutra
        elif -STOP_PCT < pct < TARGET_PCT:
            pos["target_alerted"] = False
            pos["stop_alerted"]   = False

    if changed:
        save_data(data)

def _close(data, pos_id, close_price, reason):
    idx = next((i for i, p in enumerate(data["positions"]) if p["id"] == pos_id), None)
    if idx is None: return
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
    await update.message.reply_text(
        "👋 *CryptoAdvisor Bot attivo!*\n\n"
        "🔔 Ricevi notifiche automatiche solo quando:\n"
        "   • Viene identificato un segnale BUY forte\n"
        "   • Una tua posizione raggiunge target o stop\n\n"
        "*Comandi:*\n"
        "📡 `/segnali` — analisi mercato ora\n"
        "💵 `/prezzo BTC` — prezzo singolo\n"
        "💵 `/prezzo` — tutti i prezzi\n"
        "💰 `/compra LINK 100` — apri posizione al prezzo attuale\n"
        "📥 `/aggiungi LINK 18.50 100` — aggiungi con prezzo manuale\n"
        "🔴 `/vendi LINK` — chiudi posizione\n"
        "📊 `/portafoglio` — posizioni + P&L live\n"
        "📋 `/storico` — operazioni chiuse\n"
        "⚙️ `/budget 400` — imposta budget",
        parse_mode="Markdown"
    )

async def cmd_prezzo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("⏳ Recupero tutti i prezzi...")
        prices = await fetch_all_prices()
        if not prices:
            await update.message.reply_text("❌ Errore API. Riprova."); return
        lines = ["💵 *PREZZI ATTUALI*\n"]
        for coin_id, name, short in COINS:
            e = prices.get(coin_id)
            if e:
                ch = e.get("usd_24h_change", 0) or 0
                lines.append(f"{'🟢'if ch>=0 else'🔴'} *{short}*: {fp(e['usd'])}  ({ch:+.2f}%)")
        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
        return

    short   = ctx.args[0].upper()
    coin_id = SHORT_TO_ID.get(short)
    if not coin_id:
        await update.message.reply_text(
            f"❌ '{short}' non trovato.\nUsa: BTC ETH SOL XRP ADA DOGE AVAX LINK DOT LTC MATIC BNB"
        ); return

    price = await fetch_single_price(coin_id)
    if price is None:
        await update.message.reply_text("❌ Errore prezzo."); return

    data  = load_data()
    pos   = next((p for p in data["positions"] if p["short"] == short), None)
    lines = [f"💵 *{SHORT_TO_NAME[short]}* ({short})\nPrezzo: *{fp(price)}*"]
    if pos:
        pct = (price - pos["entry_price"]) / pos["entry_price"] * 100
        pnl = pos["amount"] * pct / 100
        lines += [
            f"\n{'📈'if pct>=0 else'📉'} *Posizione aperta*",
            f"Entry: {fp(pos['entry_price'])}",
            f"P&L: *{pct:+.2f}%* ({fe(pnl)})",
            f"🎯 {fp(pos['target_price'])}  |  🛑 {fp(pos['stop_price'])}",
        ]
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

async def cmd_segnali(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔍 Analisi in corso... (30-40 secondi)")
    await scan_and_notify(ctx.bot, notify_empty=True)

async def cmd_compra(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Apre posizione al prezzo di mercato attuale."""
    if len(ctx.args) < 2:
        await update.message.reply_text(
            "❌ Uso: `/compra LINK 100`\n"
            "Per usare un prezzo manuale: `/aggiungi LINK 18.50 100`",
            parse_mode="Markdown"
        ); return

    short = ctx.args[0].upper()
    try:    amount = float(ctx.args[1])
    except: await update.message.reply_text("❌ Importo non valido"); return

    coin_id = SHORT_TO_ID.get(short)
    if not coin_id:
        await update.message.reply_text(f"❌ '{short}' non trovato."); return

    data     = load_data()
    invested = sum(p["amount"] for p in data["positions"])
    avail    = data["budget"] - invested
    if amount > avail:
        await update.message.reply_text(
            f"⚠️ Budget insufficiente.\nDisponibile: €{avail:.2f} | Richiesto: €{amount:.2f}"
        ); return

    await update.message.reply_text(f"⏳ Recupero prezzo attuale {short}...")
    price = await fetch_single_price(coin_id)
    if price is None:
        await update.message.reply_text("❌ Errore prezzo. Riprova."); return

    _apri_posizione(data, coin_id, SHORT_TO_NAME[short], short, price, amount)
    save_data(data)

    await update.message.reply_text(
        f"✅ *Posizione aperta!*\n\n"
        f"*{SHORT_TO_NAME[short]}* ({short})\n"
        f"💵 Entry: {fp(price)}\n"
        f"💶 Investito: €{amount:.2f}\n\n"
        f"🎯 Target: {fp(price*(1+TARGET_PCT/100))} (+{TARGET_PCT}%) → *+€{amount*TARGET_PCT/100:.2f}*\n"
        f"🛑 Stop: {fp(price*(1-STOP_PCT/100))} (-{STOP_PCT}%) → *-€{amount*STOP_PCT/100:.2f}*\n\n"
        f"_Monitoraggio ogni 5 minuti. Ti avviso io quando agire._",
        parse_mode="Markdown"
    )

async def cmd_aggiungi(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """
    Aggiunge una posizione con prezzo di acquisto manuale.
    Uso: /aggiungi LINK 18.50 100
         /aggiungi BTC 61000 50
    """
    if len(ctx.args) < 3:
        await update.message.reply_text(
            "❌ Uso: `/aggiungi LINK 18.50 100`\n\n"
            "• `LINK` = simbolo crypto\n"
            "• `18.50` = tuo prezzo di acquisto in $\n"
            "• `100` = importo investito in €",
            parse_mode="Markdown"
        ); return

    short = ctx.args[0].upper()
    try:
        entry_price = float(ctx.args[1])
        amount      = float(ctx.args[2])
    except:
        await update.message.reply_text("❌ Prezzo o importo non valido."); return

    if entry_price <= 0 or amount <= 0:
        await update.message.reply_text("❌ Prezzo e importo devono essere positivi."); return

    coin_id = SHORT_TO_ID.get(short)
    if not coin_id:
        await update.message.reply_text(
            f"❌ '{short}' non trovato.\n"
            f"Disponibili: {', '.join(SHORT_TO_ID.keys())}"
        ); return

    data = load_data()

    # Controlla se esiste già una posizione aperta sulla stessa crypto
    existing = next((p for p in data["positions"] if p["short"] == short), None)
    if existing:
        await update.message.reply_text(
            f"⚠️ Hai già una posizione aperta su {short}.\n"
            f"Entry attuale: {fp(existing['entry_price'])} | Investito: €{existing['amount']:.2f}\n\n"
            f"Chiudila prima con `/vendi {short}` se vuoi aggiornare.",
            parse_mode="Markdown"
        ); return

    _apri_posizione(data, coin_id, SHORT_TO_NAME[short], short, entry_price, amount)
    save_data(data)

    # Recupera prezzo attuale per mostrare P&L immediato
    await update.message.reply_text(f"⏳ Recupero prezzo attuale per confronto...")
    current = await fetch_single_price(coin_id)
    pct_now = ((current - entry_price) / entry_price * 100) if current else None
    pnl_now = (amount * pct_now / 100) if pct_now is not None else None

    msg = (
        f"📥 *Posizione aggiunta manualmente!*\n\n"
        f"*{SHORT_TO_NAME[short]}* ({short})\n"
        f"💵 Tuo prezzo d'acquisto: {fp(entry_price)}\n"
        f"💶 Investito: €{amount:.2f}\n\n"
        f"🎯 Target: {fp(entry_price*(1+TARGET_PCT/100))} (+{TARGET_PCT}%) → *+€{amount*TARGET_PCT/100:.2f}*\n"
        f"🛑 Stop: {fp(entry_price*(1-STOP_PCT/100))} (-{STOP_PCT}%) → *-€{amount*STOP_PCT/100:.2f}*\n"
    )
    if current and pct_now is not None:
        em = "📈" if pct_now >= 0 else "📉"
        msg += (
            f"\n{em} *P&L attuale*: {fp(current)} ({pct_now:+.2f}%) → *{fe(pnl_now)}*"
        )
    msg += "\n\n_Monitoraggio attivo. Ti avviso quando raggiunge target o stop._"
    await update.message.reply_text(msg, parse_mode="Markdown")

def _apri_posizione(data, coin_id, name, short, entry_price, amount):
    """Helper condiviso da /compra e /aggiungi."""
    pos = {
        "id":             len(data["positions"]) + len(data["closed"]) + 1,
        "coin_id":        coin_id,
        "name":           name,
        "short":          short,
        "entry_price":    entry_price,
        "current_price":  entry_price,
        "amount":         amount,
        "target_price":   round(entry_price * (1 + TARGET_PCT/100), 8),
        "stop_price":     round(entry_price * (1 - STOP_PCT/100), 8),
        "opened_at":      datetime.now().strftime("%d/%m %H:%M"),
        "target_alerted": False,
        "stop_alerted":   False,
    }
    data["positions"].append(pos)

async def cmd_vendi(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("❌ Uso: `/vendi LINK`", parse_mode="Markdown"); return

    short = ctx.args[0].upper()
    data  = load_data()
    pos   = next((p for p in data["positions"] if p["short"] == short), None)
    if not pos:
        await update.message.reply_text(f"❌ Nessuna posizione aperta su {short}."); return

    await update.message.reply_text(f"⏳ Recupero prezzo attuale {short}...")
    price = await fetch_single_price(pos["coin_id"])
    if price is None:
        await update.message.reply_text("❌ Errore prezzo. Riprova."); return

    _close(data, pos["id"], price, "manual")
    save_data(data)

    pct = (price - pos["entry_price"]) / pos["entry_price"] * 100
    pnl = pos["amount"] * pct / 100
    await update.message.reply_text(
        f"🔴 *Posizione chiusa* — {pos['name']}\n\n"
        f"Entry:  {fp(pos['entry_price'])}\n"
        f"Exit:   {fp(price)}\n"
        f"{'📈'if pnl>=0 else'📉'} *{pct:+.2f}%* → *{fe(pnl)}*\n\n"
        f"P&L totale realizzato: *{fe(data['pnl_total'])}*",
        parse_mode="Markdown"
    )

async def cmd_portafoglio(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    data     = load_data()
    invested = sum(p["amount"] for p in data["positions"])
    lines    = [
        "📊 *PORTAFOGLIO*\n",
        f"💰 Budget: €{data['budget']:.2f}",
        f"📌 Investito: €{invested:.2f}",
        f"✅ Disponibile: €{data['budget']-invested:.2f}",
        f"📈 P&L realizzato: *{fe(data['pnl_total'])}*\n",
    ]

    if not data["positions"]:
        lines.append("_Nessuna posizione aperta._")
    else:
        ids = ",".join({p["coin_id"] for p in data["positions"]})
        prices = {}
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(
                    f"https://api.coingecko.com/api/v3/simple/price?ids={ids}&vs_currencies=usd",
                    timeout=aiohttp.ClientTimeout(total=10)
                ) as r:
                    if r.status == 200:
                        d = await r.json()
                        prices = {k: v["usd"] for k, v in d.items()}
        except Exception as e:
            logger.error(f"portafoglio fetch: {e}")

        lines.append("*Posizioni aperte:*")
        unrealized = 0.0
        for p in data["positions"]:
            price = prices.get(p["coin_id"])
            if price:
                pct = (price - p["entry_price"]) / p["entry_price"] * 100
                pnl = p["amount"] * pct / 100
                unrealized += pnl
                em  = "🟢" if pnl >= 0 else "🔴"
                lines.append(
                    f"{em} *{p['short']}* €{p['amount']:.0f} → {pct:+.2f}% (*{fe(pnl)}*)\n"
                    f"   Entry {fp(p['entry_price'])} → Ora {fp(price)}\n"
                    f"   🎯{fp(p['target_price'])}  🛑{fp(p['stop_price'])}"
                )
            else:
                lines.append(f"⚠️ *{p['short']}* — prezzo non disponibile")

        if unrealized != 0:
            lines.append(f"\n📊 P&L non realizzato: *{fe(unrealized)}*")
            lines.append(f"💼 P&L totale (real.+non real.): *{fe(data['pnl_total']+unrealized)}*")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

async def cmd_storico(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    data = load_data()
    if not data["closed"]:
        await update.message.reply_text("📋 Nessuna operazione chiusa."); return
    lines = ["📋 *ULTIME OPERAZIONI CHIUSE*\n"]
    for op in data["closed"][:10]:
        r  = {"target":"🎯","stop":"🛑","manual":"👤"}.get(op.get("close_reason",""),"")
        em = "✅" if op["pnl"] >= 0 else "❌"
        lines.append(
            f"{em} {r} *{op['short']}* — {op.get('closed_at','')}\n"
            f"   {op['pct']:+.2f}% → *{fe(op['pnl'])}*\n"
            f"   {fp(op['entry_price'])} → {fp(op['close_price'])}"
        )
    lines.append(f"\n💰 P&L totale realizzato: *{fe(data['pnl_total'])}*")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

async def cmd_budget(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("❌ Uso: `/budget 400`", parse_mode="Markdown"); return
    try:    b = float(ctx.args[0])
    except: await update.message.reply_text("❌ Valore non valido"); return
    data = load_data()
    data["budget"] = b
    save_data(data)
    await update.message.reply_text(f"✅ Budget impostato a €{b:.2f}")

# ── Job periodici ─────────────────────────────────────────────────────────────
async def job_scan(ctx: ContextTypes.DEFAULT_TYPE):
    # notify_empty=False → silenzioso se nessun BUY forte
    await scan_and_notify(ctx.bot, notify_empty=False)

async def job_monitor(ctx: ContextTypes.DEFAULT_TYPE):
    await monitor_positions(ctx.bot)

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    app = Application.builder().token(TOKEN).build()
    for cmd, fn in [
        ("start",       cmd_start),
        ("aiuto",       cmd_start),
        ("prezzo",      cmd_prezzo),
        ("segnali",     cmd_segnali),
        ("compra",      cmd_compra),
        ("aggiungi",    cmd_aggiungi),
        ("vendi",       cmd_vendi),
        ("portafoglio", cmd_portafoglio),
        ("storico",     cmd_storico),
        ("budget",      cmd_budget),
    ]:
        app.add_handler(CommandHandler(cmd, fn))

    jq = app.job_queue
    jq.run_repeating(job_scan,    interval=30*60, first=10)
    jq.run_repeating(job_monitor, interval=5*60,  first=30)

    logger.info("Bot avviato v4 ✅")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
