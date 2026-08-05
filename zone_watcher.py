#!/usr/bin/env python3
"""
Zone Watcher v2 — мониторинг торговых зон каждые 5 минут.
Читает сетапы из setups.json (в репо).
При SL / TP2 / expire — авто-удаляет сетап из setups.json через GitHub API.
"""

import os
import json
import base64
import requests
from datetime import datetime, timezone

TOKEN    = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID  = os.environ["TELEGRAM_CHAT_ID"]
GH_TOKEN = os.environ.get("GH_TOKEN", "")
GH_REPO  = os.environ.get("GITHUB_REPOSITORY", "gusar181212-cell/orb-scanner")

SETUPS_FILE = "setups.json"
API_URL     = f"https://api.github.com/repos/{GH_REPO}/contents/{SETUPS_FILE}"
GH_HEADERS  = {
    "Authorization": f"Bearer {GH_TOKEN}",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}


def load_setups():
    r = requests.get(API_URL, headers=GH_HEADERS, timeout=10)
    if r.status_code != 200:
        print(f"  ERROR load setups: {r.status_code}")
        return [], None
    data = r.json()
    content = base64.b64decode(data["content"]).decode("utf-8")
    return json.loads(content), data["sha"]


def save_setups(setups, sha, commit_msg):
    body = json.dumps(setups, ensure_ascii=False, indent=2)
    encoded = base64.b64encode(body.encode("utf-8")).decode("utf-8")
    r = requests.put(
        API_URL,
        headers={**GH_HEADERS, "Content-Type": "application/json"},
        json={"message": commit_msg, "content": encoded, "sha": sha},
        timeout=15,
    )
    ok = r.status_code in (200, 201)
    print(f"  {'OK' if ok else 'ERR'} save_setups: {r.status_code} | {commit_msg}")
    return ok


def get_okx(symbol):
    inst = symbol.replace("USDT", "") + "-USDT-SWAP"
    try:
        t = requests.get(
            f"https://www.okx.com/api/v5/market/ticker?instId={inst}", timeout=10
        ).json()
        price = float(t["data"][0]["last"])
        c = requests.get(
            f"https://www.okx.com/api/v5/market/candles?instId={inst}&bar=15m&limit=2",
            timeout=10,
        ).json()
        prev = float(c["data"][1][4])
        return price, prev
    except Exception as e:
        print(f"  WARNING OKX {symbol}: {e}")
        return None, None


def send_tg(text):
    try:
        requests.post(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"},
            timeout=10,
        ).raise_for_status()
    except Exception as e:
        print(f"  WARNING Telegram: {e}")


def main():
    now     = datetime.now(timezone.utc)
    now_str = now.strftime("%d.%m %H:%M UTC")

    setups, sha = load_setups()

    if not setups:
        print(f"SETUPS пуст — нечего мониторить ({now_str})")
        return

    to_remove = set()

    for i, s in enumerate(setups):
        sym  = s["symbol"]
        dirn = s["direction"]

        # ── Срок жизни истёк ──────────────────────────────────────────────────
        if "expires_date" in s:
            exp = datetime.fromisoformat(s["expires_date"]).replace(tzinfo=timezone.utc)
            if now > exp:
                print(f"  {sym} — истёк {s['expires_date']}")
                send_tg(
                    f"\u23f0 <b>СЕТАП ИСТЁК</b> {sym} {dirn}\n"
                    f"Срок: {s['expires_date']} | авто-удалён\n\u23f0 {now_str}"
                )
                to_remove.add(i)
                continue

        price, prev = get_okx(sym)
        if price is None:
            continue

        zl  = float(s["zone_low"])
        zh  = float(s["zone_high"])
        sl  = float(s.get("sl",  zl - (zh - zl)))
        tp1 = s.get("tp1")
        tp2 = s.get("tp2")
        if tp1: tp1 = float(tp1)
        if tp2: tp2 = float(tp2)

        in_zone = zl <= price <= zh
        was_in  = zl <= prev  <= zh

        print(f"  {sym} {dirn}: {'IN ZONE' if in_zone else f'{price:.5f}'} | prev={prev:.5f} | sl={sl} tp1={tp1} tp2={tp2}")

        # ── Цена зашла в зону ─────────────────────────────────────────────────
        if in_zone and not was_in:
            send_tg(
                f"\U0001f7e0 <b>ЗОНА!</b> {sym} {dirn}\n"
                f"Цена: <b>{price:.5f}</b> | Зона: {zl}\u2013{zh}\n"
                f"\U0001f4cb {s.get('desc','')}\n"
                f"\u2192 Открой TV, жди 15M слабость \u2192 1M триггер!\n"
                f"\u23f0 {now_str}"
            )

        # ── LONG: пробой зоны вверх (триггер входа) ──────────────────────────
        elif dirn == "LONG" and was_in and price > zh:
            send_tg(
                f"\u26a1 <b>ВХОД LONG?</b> {sym}\n"
                f"Цена: <b>{price:.5f}</b> выше зоны ({zh})\n"
                f"\u2192 Проверь 1M close выше {zh}\n"
                f"\u23f0 {now_str}"
            )

        # ── SHORT: пробой зоны вниз (триггер входа) ──────────────────────────
        elif dirn == "SHORT" and was_in and price < zl:
            send_tg(
                f"\u26a1 <b>ВХОД SHORT?</b> {sym}\n"
                f"Цена: <b>{price:.5f}</b> ниже зоны ({zl})\n"
                f"\u2192 Проверь 1M close ниже {zl}\n"
                f"\u23f0 {now_str}"
            )

        # ── SL пробит → алерт + авто-удаление ────────────────────────────────
        sl_hit = (dirn == "LONG"  and price < sl and prev >= sl) or \
                 (dirn == "SHORT" and price > sl and prev <= sl)
        if sl_hit:
            send_tg(
                f"\U0001f534 <b>SL ПРОБИТ — сетап удалён</b>\n"
                f"{sym} {dirn} | Цена: <b>{price:.5f}</b> | SL был: {sl}\n"
                f"\u23f0 {now_str}"
            )
            to_remove.add(i)

        # ── TP1 достигнут ─────────────────────────────────────────────────────
        if tp1 and dirn == "LONG" and price >= tp1 and prev < tp1:
            send_tg(
                f"\u2705 <b>TP1!</b> {sym}\n"
                f"Цена: <b>{price:.5f}</b> \u2265 TP1 {tp1}\n"
                f"\u2192 Зафиксируй 35\u201350%, двигай SL в б/у!\n"
                f"\u23f0 {now_str}"
            )
        if tp1 and dirn == "SHORT" and price <= tp1 and prev > tp1:
            send_tg(
                f"\u2705 <b>TP1!</b> {sym}\n"
                f"Цена: <b>{price:.5f}</b> \u2264 TP1 {tp1}\n"
                f"\u2192 Зафиксируй 35\u201350%, двигай SL в б/у!\n"
                f"\u23f0 {now_str}"
            )

        # ── TP2 достигнут → авто-удаление ────────────────────────────────────
        tp2_hit = (tp2 and dirn == "LONG"  and price >= tp2 and prev < tp2) or \
                  (tp2 and dirn == "SHORT" and price <= tp2 and prev > tp2)
        if tp2_hit:
            send_tg(
                f"\U0001f3c6 <b>TP2 ДОСТИГНУТ — сетап удалён</b>\n"
                f"{sym} {dirn} | Цена: <b>{price:.5f}</b> | TP2: {tp2}\n"
                f"\u2192 Закрой остаток позиции!\n"
                f"\u23f0 {now_str}"
            )
            to_remove.add(i)

    # ── Авто-удаление из setups.json ─────────────────────────────────────────
    if to_remove and sha:
        removed = [setups[i]["symbol"] for i in sorted(to_remove)]
        new_setups = [s for i, s in enumerate(setups) if i not in to_remove]
        save_setups(new_setups, sha, f"auto-remove: {', '.join(removed)}")
        print(f"  Удалены из setups.json: {removed}")
    else:
        print(f"OK - no events ({now_str})")


if __name__ == "__main__":
    main()
