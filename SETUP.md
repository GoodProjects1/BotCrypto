# 🤖 CryptoAdvisor Bot — Guida Setup (10 minuti)

## STEP 1 — Crea il tuo Bot Telegram (2 min)

1. Apri Telegram e cerca **@BotFather**
2. Invia `/newbot`
3. Dai un nome al bot, es: `MioCryptoBot`
4. Dai uno username, es: `mio_crypto_advisor_bot`
5. BotFather ti manderà il **TOKEN** — copialo, es:
   `1234567890:AAFxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx`

---

## STEP 2 — Trova il tuo Chat ID (1 min)

1. Cerca **@userinfobot** su Telegram
2. Invia `/start`
3. Ti risponde con il tuo **ID**, es: `123456789`
   → Copialo

---

## STEP 3 — Deploy su Railway (5 min)

1. Vai su **railway.app** → crea account gratuito (con GitHub)
2. Clicca **"New Project"** → **"Deploy from GitHub repo"**
3. Carica i 3 file (bot.py, requirements.txt, railway.toml)
   oppure usa GitHub:
   - Crea repo su github.com → carica i 3 file → collega a Railway
4. In Railway, vai su **"Variables"** e aggiungi:
   ```
   TELEGRAM_TOKEN = il-tuo-token-da-BotFather
   CHAT_ID        = il-tuo-id-da-userinfobot
   ```
5. Clicca **Deploy** → aspetta 1-2 minuti

---

## STEP 4 — Avvia il bot (30 sec)

1. Vai su Telegram, trova il tuo bot
2. Invia `/start`
3. Dovresti ricevere il messaggio di benvenuto ✅

---

## Comandi disponibili

| Comando | Cosa fa |
|---------|---------|
| `/start` | Messaggio di benvenuto + lista comandi |
| `/segnali` | Analisi immediata delle crypto |
| `/compra BTC 50` | Apre posizione €50 su Bitcoin |
| `/vendi BTC` | Chiude posizione su Bitcoin |
| `/portafoglio` | Vedi posizioni aperte + P&L live |
| `/storico` | Ultime operazioni chiuse |
| `/budget 400` | Imposta il tuo budget totale |
| `/aiuto` | Mostra la lista comandi |

---

## Come funziona il bot automatico

- Ogni **30 minuti** analizza 12 crypto con RSI, MACD, Bollinger Bands
- Se trova segnali BUY forti (forza ≥ 55%) → ti manda notifica
- Ogni **5 minuti** controlla le tue posizioni aperte
- Se raggiungi **+4.5%** → ti avvisa: è ora di vendere 🎯
- Se scendi **-3%** → stop loss: ti avvisa per proteggere il capitale 🛑

---

## Esempio di sessione tipica

```
[Bot] 📡 SEGNALI BUY RILEVATI
      🟢 Solana (SOL) — Prezzo: $145.20 — RSI: 32 — Forza: 72%

[Tu]  /compra SOL 50

[Bot] ✅ Posizione aperta!
      Entry: $145.20 | Target: $151.73 (+4.5%) → +€2.25
      Stop: $140.85 (-3%) → -€1.50

--- alcune ore dopo ---

[Bot] 🎯 TARGET RAGGIUNTO!
      SOL: +4.52% → +€2.26
      👉 /vendi SOL per chiudere

[Tu]  /vendi SOL

[Bot] 🔴 Posizione chiusa — Solana
      +4.52% → +€2.26 ✅
```

---

## Note importanti

- **Railway piano gratuito**: 500 ore/mese — sufficiente per 1 bot
- **I dati** sono salvati nel file `data.json` sul server Railway
- **Il bot NON esegue ordini reali** — sei tu a comprare/vendere sul tuo exchange
- Usa sempre solo fondi che puoi permetterti di perdere ⚠️
