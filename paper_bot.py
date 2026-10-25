"""
Paper bot memecoin Solana - SOLO SIMULAZIONE.
Nessun wallet, nessuna chiave privata, nessuna transazione reale.

Modalita' continua (consigliata): con LOOP_MINUTES>0 il programma resta attivo e ripete il ciclo
ogni LOOP_INTERVAL_SEC secondi. Senza, esegue un solo ciclo.

Ad ogni ciclo:
1. gestisce l'eventuale posizione virtuale aperta (take profit, stop loss, tempo massimo,
   protezione del profitto: attiva a ROI netto +15%, vende se scende sotto +14%);
2. se non ha posizioni aperte cerca subito il prossimo candidato;
3. salva stato in state.json e trade chiusi in trades.csv;
4. avvisa su Telegram (se i secrets sono impostati): un messaggio "live" che si aggiorna
   ad ogni ciclo con P&L in $ e ROI %, piu' un messaggio di chiusura.

Dati: DEX Screener (prezzi, liquidita') e RugCheck (rischi token). Entrambi senza chiave.
ATTENZIONE: gli endpoint non sono stati testati dal vivo da chi ha scritto lo script;
controlla i primi log nella scheda Actions.
"""
import csv
import json
import math
import os
import subprocess
import time
from datetime import datetime, timezone

import requests

try:
    from zoneinfo import ZoneInfo
    LOCAL_TZ = ZoneInfo("Europe/Rome")
except Exception:
    LOCAL_TZ = timezone.utc

# ---------------- PARAMETRI (modificabili) ----------------
START_CAPITAL = 5.0          # saldo virtuale iniziale
POSITION_SIZE = 4.0          # importo investito in ogni posizione (sempre lo stesso, una alla volta)
RESET_ID = "2026-10-25-saldo-5"   # se cambia, il saldo riparte da START_CAPITAL (lo storico dei trade resta)
TAKE_PROFIT = 0.40           # +40% dal prezzo osservato all'ingresso
STOP_LOSS = 0.10             # -10% dal prezzo osservato all'ingresso
MAX_HOLD_MIN = 90            # uscita a tempo
ARM_ROI = 15.0               # ROI netto % (dopo costi) a cui si attiva la protezione del profitto
FLOOR_ROI = 14.0             # con protezione attiva: vende se il ROI netto scende sotto questo valore
FEE = 0.003                  # commissione stimata per lato (0,3%)
FIXED_COST = 0.03            # costo fisso stimato per trade (priority fee, rent...)
ENTRY_DELAY_PENALTY = 0.015  # peggioramento prezzo stimato per il ritardo d'ingresso (1,5%)
EXIT_DELAY_PENALTY = 0.01    # peggioramento stimato in uscita (1%)

MIN_LIQUIDITY_USD = 15000
MIN_VOLUME_H1 = 10000
MIN_AGE_MIN = 30
MAX_AGE_MIN = 24 * 60
MIN_CHANGE_M5 = 0.0          # variazione % ultimi 5 minuti
MIN_CHANGE_H1 = 5.0          # variazione % ultima ora
MAX_CHANGE_H1 = 150.0        # evita di comprare dopo pump troppo estesi
MIN_BUY_SELL_RATIO = 1.2     # acquisti/vendite ultima ora

LOOP_MIN = float(os.environ.get("LOOP_MINUTES", "0") or 0)
MISS_LIMIT = 5 if LOOP_MIN > 0 else 2   # controlli consecutivi senza prezzo prima di dichiarare la perdita totale

STATE_FILE = "state.json"
TRADES_FILE = "trades.csv"
DEX = "https://api.dexscreener.com"
RUG = "https://api.rugcheck.xyz/v1"
TRADE_FIELDS = [
    "chiuso_il", "simbolo", "mint", "aperto_il", "minuti", "prezzo_ingresso",
    "prezzo_uscita", "importo", "incasso", "pnl", "pnl_pct", "motivo", "liquidita_ingresso",
]


def now():
    return datetime.now(timezone.utc)


def iso(dt):
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def get_json(url, retries=2):
    for i in range(retries + 1):
        try:
            r = requests.get(url, timeout=15, headers={"User-Agent": "paper-bot/1.0"})
            if r.status_code == 200:
                return r.json()
            print(f"HTTP {r.status_code} da {url}")
        except Exception as e:  # rete, JSON, ecc.
            print(f"Errore su {url}: {e}")
        time.sleep(1.5 * (i + 1))
    return None


def tg_call(method, payload):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        return None
    try:
        r = requests.post(f"https://api.telegram.org/bot{token}/{method}", json=payload, timeout=15)
        return r.json()
    except Exception as e:
        # non stampiamo l'errore completo: potrebbe contenere l'URL con il token
        print("Telegram non raggiungibile:", type(e).__name__)
        return None


def send_telegram(text):
    """Invia un messaggio e restituisce il suo id (o None)."""
    print("TELEGRAM:", text.replace("\n", " | "))
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if not chat:
        return None
    res = tg_call("sendMessage", {"chat_id": chat, "text": text})
    try:
        return res["result"]["message_id"]
    except Exception:
        return None


def edit_telegram(message_id, text):
    """Modifica un messaggio gia' inviato. True se riuscito."""
    print("TELEGRAM (modifica):", text.replace("\n", " | "))
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if not chat or not message_id:
        return False
    res = tg_call("editMessageText", {"chat_id": chat, "message_id": message_id, "text": text})
    if res and res.get("ok"):
        return True
    return bool(res and "not modified" in str(res.get("description", "")))


def money(x):
    return f"{'+' if x >= 0 else '-'}${abs(x):.2f}"


# ---------------- STATO ----------------
def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {"balance": START_CAPITAL, "reset_id": RESET_ID, "position": None, "recent": [], "scans": 0, "skipped": {}}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


def ensure_trades_file():
    if not os.path.exists(TRADES_FILE):
        with open(TRADES_FILE, "w", newline="", encoding="utf-8") as f:
            csv.DictWriter(f, fieldnames=TRADE_FIELDS).writeheader()


def append_trade(row):
    ensure_trades_file()
    with open(TRADES_FILE, "a", newline="", encoding="utf-8") as f:
        csv.DictWriter(f, fieldnames=TRADE_FIELDS).writerow(row)


def skip(state, reason):
    state["skipped"][reason] = state["skipped"].get(reason, 0) + 1


# ---------------- DATI DI MERCATO ----------------
def best_pair(mint):
    """Coppia Solana con piu' liquidita' per il token, oppure None."""
    data = get_json(f"{DEX}/latest/dex/tokens/{mint}")
    pairs = (data or {}).get("pairs") or []
    pairs = [
        p for p in pairs
        if p.get("chainId") == "solana"
        and (p.get("baseToken") or {}).get("address") == mint
        and p.get("priceUsd")
    ]
    if not pairs:
        return None
    return max(pairs, key=lambda p: (p.get("liquidity") or {}).get("usd") or 0)


def trending_mints():
    urls = [
        f"{DEX}/token-boosts/latest/v1",
        f"{DEX}/token-boosts/top/v1",
        f"{DEX}/token-profiles/latest/v1",
    ]
    seen, out = set(), []
    for u in urls:
        data = get_json(u)
        if not isinstance(data, list):
            continue
        for item in data:
            if item.get("chainId") == "solana":
                m = item.get("tokenAddress")
                if m and m not in seen:
                    seen.add(m)
                    out.append(m)
    return out


def pairs_for(mints):
    """Interroga DEX Screener a blocchi da 30 indirizzi."""
    result = []
    for i in range(0, len(mints), 30):
        chunk = mints[i:i + 30]
        data = get_json(f"{DEX}/latest/dex/tokens/{','.join(chunk)}")
        for p in (data or {}).get("pairs") or []:
            if p.get("chainId") == "solana" and p.get("priceUsd"):
                result.append(p)
    # una coppia per token: la piu' liquida
    best = {}
    for p in result:
        m = (p.get("baseToken") or {}).get("address")
        liq = (p.get("liquidity") or {}).get("usd") or 0
        if m and (m not in best or liq > ((best[m].get("liquidity") or {}).get("usd") or 0)):
            best[m] = p
    return list(best.values())


RUG_CACHE = {}


def rug_check(mint):
    """Con cache di 10 minuti, per non interrogare RugCheck a ogni ciclo."""
    hit = RUG_CACHE.get(mint)
    if hit and time.time() - hit[0] < 600:
        return hit[1]
    res = _rug_check(mint)
    RUG_CACHE[mint] = (time.time(), res)
    return res


def _rug_check(mint):
    """(ok, motivo). Dati mancanti o rischio 'danger' = scarta. Mai 'sicuro': solo 'non scartato'."""
    data = get_json(f"{RUG}/tokens/{mint}/report/summary")
    if not isinstance(data, dict) or "risks" not in data:
        return False, "rugcheck_non_verificato"
    for r in data.get("risks") or []:
        level = str(r.get("level", "")).lower()
        name = str(r.get("name", "")).lower()
        if level == "danger":
            return False, "rugcheck_danger"
        if "mint authority" in name or "freeze authority" in name:
            return False, "rugcheck_authority"
    return True, "ok"


def check_candidate(p, state):
    liq = (p.get("liquidity") or {}).get("usd") or 0
    vol1 = (p.get("volume") or {}).get("h1") or 0
    ch = p.get("priceChange") or {}
    m5, h1 = ch.get("m5"), ch.get("h1")
    tx = (p.get("txns") or {}).get("h1") or {}
    buys, sells = tx.get("buys", 0), tx.get("sells", 0)
    created = p.get("pairCreatedAt")
    if created is None:
        return "eta_sconosciuta"
    age_min = (time.time() * 1000 - created) / 60000
    if liq < MIN_LIQUIDITY_USD:
        return "liquidita_bassa"
    if vol1 < MIN_VOLUME_H1:
        return "volume_basso"
    if age_min < MIN_AGE_MIN or age_min > MAX_AGE_MIN:
        return "eta_fuori_range"
    if m5 is None or h1 is None:
        return "variazioni_mancanti"
    if m5 < MIN_CHANGE_M5:
        return "momentum_m5"
    if h1 < MIN_CHANGE_H1 or h1 > MAX_CHANGE_H1:
        return "variazione_h1_fuori_range"
    if sells and buys / sells < MIN_BUY_SELL_RATIO:
        return "pressione_vendita"
    if p["baseToken"]["address"] in state["recent"]:
        return "gia_scambiato_di_recente"
    return None


def score(p):
    ch = p.get("priceChange") or {}
    liq = (p.get("liquidity") or {}).get("usd") or 1
    vol1 = (p.get("volume") or {}).get("h1") or 0
    return (ch.get("m5") or 0) + 0.2 * (ch.get("h1") or 0) + 5 * math.log10(1 + vol1 / liq)


# ---------------- OPERAZIONI VIRTUALI ----------------
def slippage(trade_usd, liquidity_usd):
    # impatto approssimato su una pool a prodotto costante: importo / meta' liquidita'
    return min(0.5, trade_usd / max(liquidity_usd / 2, 1))


def open_position(state, p):
    liq = (p.get("liquidity") or {}).get("usd") or 0
    size = POSITION_SIZE
    price = float(p["priceUsd"])
    entry_eff = price * (1 + FEE + slippage(size, liq) + ENTRY_DELAY_PENALTY)
    units = (size - FIXED_COST) / entry_eff
    mint = p["baseToken"]["address"]
    state["position"] = {
        "mint": mint,
        "symbol": p["baseToken"].get("symbol", "?"),
        "opened": iso(now()),
        "opened_ts": time.time(),
        "signal_price": price,
        "entry_eff": entry_eff,
        "units": units,
        "size": size,
        "liq_entry": liq,
        "misses": 0,
    }
    state["recent"] = (state["recent"] + [mint])[-20:]
    pos = state["position"]
    pos["tg_msg"] = send_telegram(
        f"APERTA (virtuale) ${pos['symbol']}\n"
        f"Importo {size:.2f} | prezzo segnale {price:.8g}\n"
        f"Liquidita' {liq:,.0f} USD\n"
        f"Saldo {state['balance']:.2f} | investiti {size:.2f}\n"
        f"Questo messaggio si aggiorna ad ogni controllo."
    )


def live_status(pos, price, liq):
    """P&L in $ e ROI % se chiudesse adesso, dopo costi e slippage stimati."""
    exit_eff = price * (1 - FEE - slippage(pos["units"] * price, liq) - EXIT_DELAY_PENALTY)
    value = max(0.0, pos["units"] * exit_eff - FIXED_COST)
    pnl = value - pos["size"]
    return pnl, 100 * pnl / pos["size"]


def update_live_message(state, price, liq, change, held):
    pos = state["position"]
    pnl, roi = live_status(pos, price, liq)
    text = (
        f"LIVE ${pos['symbol']}\n"
        f"Saldo: {state['balance']:.2f} | investiti: {pos['size']:.2f}\n"
        f"P&L: {money(pnl)} | ROI: {roi:+.1f}% (dopo costi stimati)\n"
        f"Saldo se chiudesse ora: {state['balance'] + pnl:.2f}\n"
        f"Prezzo: {change:+.1%} dall'ingresso (prima dei costi)\n"
        f"Aperta da {held:.0f} min su {MAX_HOLD_MIN} | TP +{TAKE_PROFIT:.0%} / SL -{STOP_LOSS:.0%}\n"
        f"Protezione profitto: "
        + (f"ATTIVA (vende se ROI < +{FLOOR_ROI:.0f}%)" if pos.get("armed") else f"si attiva a ROI +{ARM_ROI:.0f}%")
        + "\n"
        f"Ultimo controllo: {now().astimezone(LOCAL_TZ).strftime('%H:%M')}"
    )
    if not edit_telegram(pos.get("tg_msg"), text):
        pos["tg_msg"] = send_telegram(text)


def close_position(state, price, reason, liq_now):
    pos = state["position"]
    if price is None:
        proceeds = 0.0
        exit_price = 0.0
    else:
        exit_eff = price * (1 - FEE - slippage(pos["units"] * price, liq_now) - EXIT_DELAY_PENALTY)
        proceeds = max(0.0, pos["units"] * exit_eff - FIXED_COST)
        exit_price = price
    pnl = proceeds - pos["size"]
    minutes = (time.time() - pos["opened_ts"]) / 60
    state["balance"] += pnl
    append_trade({
        "chiuso_il": iso(now()), "simbolo": pos["symbol"], "mint": pos["mint"],
        "aperto_il": pos["opened"], "minuti": round(minutes, 1),
        "prezzo_ingresso": pos["signal_price"], "prezzo_uscita": exit_price,
        "importo": round(pos["size"], 4), "incasso": round(proceeds, 4),
        "pnl": round(pnl, 4), "pnl_pct": round(100 * pnl / pos["size"], 1),
        "motivo": reason, "liquidita_ingresso": round(pos["liq_entry"]),
    })
    send_telegram(
        f"CHIUSO (virtuale) ${pos['symbol']} - {reason}\n"
        f"P&L {pnl:+.2f} ({100 * pnl / pos['size']:+.1f}%) dopo costi e slippage stimati\n"
        f"Saldo {state['balance']:.2f}"
    )
    edit_telegram(pos.get("tg_msg"), f"CHIUSA ${pos['symbol']} - risultato nel messaggio qui sotto")
    state["position"] = None


def manage_position(state):
    pos = state["position"]
    p = best_pair(pos["mint"])
    liq = ((p or {}).get("liquidity") or {}).get("usd") or 0
    if not p or liq < 1000:
        pos["misses"] += 1
        print(f"Prezzo non quotabile ({pos['misses']})")
        if pos["misses"] >= MISS_LIMIT:
            close_position(state, None, "USCITA NON QUOTABILE (perdita totale)", 0)
        return
    pos["misses"] = 0
    price = float(p["priceUsd"])
    change = price / pos["signal_price"] - 1
    held = (time.time() - pos["opened_ts"]) / 60
    pnl, roi = live_status(pos, price, liq)
    if not pos.get("armed") and roi >= ARM_ROI:
        pos["armed"] = True
        send_telegram(
            f"PROTEZIONE ATTIVA ${pos['symbol']}: ROI {roi:+.1f}% (dopo costi).\n"
            f"Resta aperta finche' sale; vende se il ROI scende sotto +{FLOOR_ROI:.0f}%."
        )
    if change >= TAKE_PROFIT:
        close_position(state, price, "take profit", liq)
    elif pos.get("armed") and roi < FLOOR_ROI:
        close_position(state, price, f"protezione profitto (ROI sceso sotto +{FLOOR_ROI:.0f}%)", liq)
    elif change <= -STOP_LOSS:
        close_position(state, price, "stop loss", liq)
    elif held >= MAX_HOLD_MIN:
        close_position(state, price, "tempo massimo", liq)
    else:
        print(f"Posizione aperta {pos['symbol']}: {change:+.1%} dopo {held:.0f} min")
        update_live_message(state, price, liq, change, held)


def find_and_open(state):
    mints = trending_mints()
    print(f"Token in tendenza trovati: {len(mints)}")
    state["scans"] += 1
    candidates = []
    for p in pairs_for(mints):
        reason = check_candidate(p, state)
        if reason:
            skip(state, reason)
        else:
            candidates.append(p)
    candidates.sort(key=score, reverse=True)
    for p in candidates[:5]:
        ok, why = rug_check(p["baseToken"]["address"])
        if not ok:
            skip(state, why)
            continue
        open_position(state, p)
        return
    print("Nessun candidato valido in questo ciclo")


def git_push():
    """In modalita' continua salva stato e storico su GitHub senza aspettare la fine del turno."""
    if not os.environ.get("GITHUB_ACTIONS"):
        return
    try:
        subprocess.run(["git", "add", "state.json", "trades.csv"], check=True, timeout=60)
        if subprocess.run(["git", "diff", "--staged", "--quiet"], timeout=60).returncode != 0:
            subprocess.run(["git", "commit", "-m", "stato paper bot"], check=True, timeout=60)
            subprocess.run(["git", "pull", "--rebase"], check=True, timeout=120)
            subprocess.run(["git", "push"], check=True, timeout=120)
    except Exception as e:
        print("Salvataggio su GitHub non riuscito:", type(e).__name__)


def cycle(state):
    if state["position"]:
        manage_position(state)
    if not state["position"]:
        find_and_open(state)


def main():
    ensure_trades_file()
    interval = int(os.environ.get("LOOP_INTERVAL_SEC", "60") or 60)
    end = time.time() + LOOP_MIN * 60
    state = load_state()
    if state.get("reset_id") != RESET_ID:
        state["balance"] = START_CAPITAL
        state["reset_id"] = RESET_ID
        state.pop("cash", None)
        print(f"Saldo riportato a {START_CAPITAL:.2f}")
    last_push = time.time()
    while True:
        before = ((state["position"] or {}).get("mint"), round(state["balance"], 4))
        try:
            cycle(state)
        except Exception as e:  # un errore in un ciclo non deve fermare il bot
            print("Errore nel ciclo:", type(e).__name__, e)
        save_state(state)
        after = ((state["position"] or {}).get("mint"), round(state["balance"], 4))
        print(f"Saldo {state['balance']:.2f} | posizione aperta: {'si' if state['position'] else 'no'}")
        if LOOP_MIN > 0 and (after != before or time.time() - last_push > 1800):
            git_push()
            last_push = time.time()
        if LOOP_MIN <= 0 or time.time() + interval >= end:
            break
        time.sleep(interval)


if __name__ == "__main__":
    main()
