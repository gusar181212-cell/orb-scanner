#!/usr/bin/env python3
"""
ORB NY Scanner — JW-bear SHORT only
Запускается в 13:45 UTC, проверяет закрытый 13:30 бар по всем 47 монетам.
Binance API (публичный, без ключей). HYPE и KAS — Bybit.
"""

import os
import time
import requests
from datetime import datetime, timezone

# ─── Список символов ────────────────────────────────────────────────────────
SYMBOLS = [
    "BTCUSDT","ETHUSDT","SOLUSDT","XRPUSDT","ADAUSDT",
    "DOGEUSDT","LINKUSDT","AVAXUSDT","DOTUSDT","LTCUSDT",
    "BCHUSDT","ATOMUSDT","NEARUSDT","APTUSDT","SUIUSDT",
    "OPUSDT","ARBUSDT","TONUSDT","FILUSDT","INJUSDT",
    "TIAUSDT","SEIUSDT","UNIUSDT","AAVEUSDT","HBARUSDT",
    "HYPEUSDT","TRXUSDT","ZECUSDT","ETCUSDT",
    "RENDERUSDT","TAOUSDT","FETUSDT","WLDUSDT","ENAUSDT",
    "ONDOUSDT","JUPUSDT","STXUSDT","ICPUSDT","XLMUSDT",
    "KASUSDT","PENGUUSDT","VIRTUALUSDT","KAITOUSDT",
    "PEPEUSDT","WIFUSDT","BONKUSDT","TRUMPUSDT",
]

# Монеты, которых нет на Binance — используем Bybit
BYBIT_ONLY = {"HYPEUSDT", "KASUSDT"}

ATR_THRESHOLD = 0.10   # бар >= 10% дневного ATR(14)
NY_HOUR, NY_MIN = 13, 30

# ─── API-запросы ─────────────────────────────────────────────────────────────
def binance_klines(symbol, interval, limit):
    url = "https://api.binance.com/api/v3/klines"
    r = requests.get(url, params={"symbol": symbol, "interval": interval, "limit": limit}, timeout=10)
    r.raise_for_status()
    return [{"time": int(k[0])//1000, "open": float(k[1]),
              "high": float(k[2]), "low": float(k[3]), "close": float(k[4])} for k in r.json()]

def bybit_klines(symbol, interval, limit):
    url = "https://api.bybit.com/v5/market/kline"
    r = requests.get(url, params={"category": "linear", "symbol": symbol,
                                   "interval": str(interval), "limit": limit}, timeout=10)
    r.raise_for_status()
    rows = r.json()["result"]["list"]
    bars = [{"time": int(row[0])//1000, "open": float(row[1]),
              "high": float(row[2]), "low": float(row[3]), "close": float(row[4])} for row in rows]
    return sorted(bars, key=lambda x: x["time"])

def get_15m(symbol, limit=5):
    if symbol in BYBIT_ONLY:
        return bybit_klines(symbol, 15, limit)
    return binance_klines(symbol, "15m", limit)

def get_daily(symbol, limit=16):
    if symbol in BYBIT_ONLY:
        return bybit_klines(symbol, "D", limit)
    return binance_klines(symbol, "1d", limit)

# ─── Логика стратегии ────────────────────────────────────────────────────────
def calc_atr14(daily_bars):
    """ATR14 из завершённых дневных баров (не считаем текущий день)."""
    ranges = [b["high"] - b["low"] for b in daily_bars[:-1]]
    window = ranges[-14:] if len(ranges) >= 14 else ranges
    return sum(window) / len(window) if window else 0.0

def is_jw_bear(b):
    """Доминирующий верхний вик → SHORT."""
    o, h, l, c = b["open"], b["high"], b["low"], b["close"]
    upper = h - max(o, c)
    lower = min(o, c) - l
    body  = abs(c - o)
    rng   = h - l
    if rng == 0:
        return False
    return upper > lower and upper > body * 0.5

def find_orb_bar(bars_15m):
    """Ищем бар 13:30 UTC среди последних 15M баров."""
    for b in reversed(bars_15m):
        t = datetime.fromtimestamp(b["time"], tz=timezone.utc)
        if t.hour == NY_HOUR and t.minute == NY_MIN:
            return b
    return None

# ─── Основной скан ───────────────────────────────────────────────────────────
def scan():
    now = datetime.now(timezone.utc)
    print(f"[{now.strftime('%Y-%m-%d %H:%M UTC')}] ORB Scanner запущен — {len(SYMBOLS)} монет\n")

    setups = []
    skipped = []
    errors  = []

    for sym in SYMBOLS:
        try:
            bars_15m = get_15m(sym, limit=6)
            orb      = find_orb_bar(bars_15m)

            if orb is None:
                skipped.append(f"{sym}: бар 13:30 не найден")
                continue

            # ATR-фильтр
            daily = get_daily(sym, limit=16)
            atr   = calc_atr14(daily)
            rng   = orb["high"] - orb["low"]
            if atr > 0 and rng < ATR_THRESHOLD * atr:
                print(f"  {sym}: ATR-фильтр — пропуск (range={rng:.6g} < 10% ATR={atr*0.1:.6g})")
                skipped.append(f"{sym}: ATR-фильтр")
                time.sleep(0.08)
                continue

            # JW-bear
            if is_jw_bear(orb):
                # Следующий бар после ORB (13:45) — точка входа
                next_bars = [b for b in bars_15m if b["time"] > orb["time"]]
                entry = next_bars[0]["open"] if next_bars else orb["close"]
                tp    = orb["low"]
                sl    = orb["high"]
                sl_dist = sl - entry
                rr    = (entry - tp) / sl_dist if sl_dist > 1e-10 else 0

                setups.append({
                    "sym":   sym,
                    "entry": entry,
                    "tp":    tp,
                    "sl":    sl,
                    "rr":    rr,
                    "range": rng,
                    "atr":   atr,
                })
                print(f"  ✅ {sym}: JW-bear SHORT | entry={entry:.6g} | TP={tp:.6g} | SL={sl:.6g} | R:R={rr:.2f}")
            else:
                print(f"  — {sym}: нет JW-bear")

            time.sleep(0.08)

        except Exception as e:
            errors.append(f"{sym}: {e}")
            print(f"  ❌ {sym}: {e}")

    print(f"\nИтого: сетапов={len(setups)}, пропущено={len(skipped)}, ошибок={len(errors)}")
    return setups, errors

# ─── Telegram ────────────────────────────────────────────────────────────────
def send_telegram(token, chat_id, setups, errors):
    if not token or not chat_id:
        print("Telegram не настроен (нет TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID).")
        return

    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    if setups:
        lines = [f"🎯 *ORB JW\\-bear SHORT* — NY {date_str}\n"]
        for s in setups:
            def fmt(v):
                return f"{v:.8g}".rstrip("0").rstrip(".")
            rr_str = f"{s['rr']:.2f}"
            lines.append(
                f"*{s['sym']}*\n"
                f"  Entry: `{fmt(s['entry'])}`\n"
                f"  TP:    `{fmt(s['tp'])}`\n"
                f"  SL:    `{fmt(s['sl'])}`\n"
                f"  R:R:   `{rr_str}`\n"
            )
        lines.append(f"\n_Всего сетапов: {len(setups)} из {len(SYMBOLS)}_")
    else:
        lines = [f"⚪ *ORB Scanner* — NY {date_str}\n\nСетапов не найдено\\."]

    if errors:
        lines.append(f"\n⚠️ Ошибки \\({len(errors)}\\): " +
                     ", ".join(e.split(":")[0] for e in errors[:5]))

    text = "\n".join(lines)
    url  = f"https://api.telegram.org/bot{token}/sendMessage"
    resp = requests.post(url, json={"chat_id": chat_id, "text": text,
                                     "parse_mode": "MarkdownV2"}, timeout=10)
    print(f"Telegram: HTTP {resp.status_code}")
    if resp.status_code != 200:
        print(resp.text)

# ─── Точка входа ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    TG_CHAT  = os.environ.get("TELEGRAM_CHAT_ID",   "")

    setups, errors = scan()
    send_telegram(TG_TOKEN, TG_CHAT, setups, errors)
