"""
Paper bot memecoin Solana - SOLO SIMULAZIONE.
Nessun wallet, nessuna chiave privata, nessuna transazione reale.

Ad ogni esecuzione (ogni ~5 minuti da GitHub Actions):
1. gestisce l'eventuale posizione virtuale aperta (take profit, stop loss, tempo massimo);
2. se non ha posizioni aperte cerca subito il prossimo candidato;
3. salva stato in state.json e trade chiusi in trades.csv;
4. avvisa su Telegram (se i secrets sono impostati).

Dati: DEX Screener (prezzi, liquidita') e RugCheck (rischi token). Entrambi senza chiave.
ATTENZIONE: gli endpoint non sono stati testati dal vivo da chi ha scritto lo script;
controlla i primi log nella scheda Actions.
"""
import csv
import json
import math
import os
import time
from datetime import datetime, timezone

import requests

# ---------------- PARAMETRI (modificabili) ----------------
START_CAPITAL = 5.0          # capitale virtuale iniziale
POSITION_SIZE = 4.0          # importo massimo per posizione (una sola posizione alla volta)
TAKE_PROFIT = 0.40           # +40% dal prezzo osservato all'ingresso
STOP_LOSS = 0.20             # -20%
MAX_HOLD_MIN = 90            # uscita a tempo
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


def send_telegram(text):
    print("TELEGRAM:", text.replace("\n", " | "))
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat, "text": text},
            timeout=15,
        )
    except Exception as e:
        print("Telegram non inviato:", e)


# ---------------- STATO ----------------
def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {"cash": START_CAPITAL, "position": None, "recent": [], "scans": 0, "skipped": {}}


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


def rug_check(mint):
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
    size = min(POSITION_SIZE, state["cash"])
    if size <= FIXED_COST * 2:
        return
    price = float(p["priceUsd"])
    entry_eff = price * (1 + FEE + slippage(size, liq) + ENTRY_DELAY_PENALTY)
    units = (size - FIXED_COST) / entry_eff
    mint = p["baseToken"]["address"]
    state["cash"] -= size
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
    send_telegram(
        f"APERTO (virtuale) ${state['position']['symbol']}\n"
        f"Prezzo segnale {price:.8g} | importo {size:.2f}\n"
        f"Liquidita' {liq:,.0f} USD | cassa {state['cash']:.2f}"
    )


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
    state["cash"] += proceeds
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
        f"Cassa {state['cash']:.2f}"
    )
    state["position"] = None


def manage_position(state):
    pos = state["position"]
    p = best_pair(pos["mint"])
    liq = ((p or {}).get("liquidity") or {}).get("usd") or 0
    if not p or liq < 1000:
        pos["misses"] += 1
        print(f"Prezzo non quotabile ({pos['misses']})")
        if pos["misses"] >= 2:
            close_position(state, None, "USCITA NON QUOTABILE (perdita totale)", 0)
        return
    pos["misses"] = 0
    price = float(p["priceUsd"])
    change = price / pos["signal_price"] - 1
    held = (time.time() - pos["opened_ts"]) / 60
    if change >= TAKE_PROFIT:
        close_position(state, price, "take profit", liq)
    elif change <= -STOP_LOSS:
        close_position(state, price, "stop loss", liq)
    elif held >= MAX_HOLD_MIN:
        close_position(state, price, "tempo massimo", liq)
    else:
        print(f"Posizione aperta {pos['symbol']}: {change:+.1%} dopo {held:.0f} min")


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


def main():
    ensure_trades_file()
    state = load_state()
    if state["position"]:
        manage_position(state)
    if not state["position"]:
        find_and_open(state)
    save_state(state)
    equity = state["cash"] + (state["position"]["size"] if state["position"] else 0)
    print(f"Cassa {state['cash']:.2f} | patrimonio (a costo) {equity:.2f}")


if __name__ == "__main__":
    main()
