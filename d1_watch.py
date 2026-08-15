# -*- coding: utf-8 -*-
"""D1 Watch v1.0 — автоматический сканер модуля «Моментум D1 + режимный фильтр».

Модуль из скила novaya-ts: +0.447R, PF 1.93, n=142, холдаут +0.515R.
По годам не стареет: 2024 +0.350R, 2025 +0.368R, 2026 +0.515R.
Работает ТОЛЬКО на 8 мажорах — на других монетах даёт ноль (проверено 08.08.2026).

ЧТО ДЕЛАЕТ
  * раз в сутки, после закрытия дневного бара (00:00 UTC), сканирует 8 монет;
  * проверяет режим рынка — без него модуль не торгуется;
  * шлёт сигнал в Telegram с готовыми уровнями и издержками;
  * ВЕДЁТ ОТКРЫТЫЕ ПОЗИЦИИ: каждый день проверяет SL / TP / тайм-стоп
    и присылает сообщение о выходе с результатом в R;
  * не даёт новый сигнал по монете, пока предыдущая сделка не закрыта —
    это условие бэктеста, без него статистика поедет.

ЗАПУСК (Windows, песочница OKX не достаёт):
    python C:\\Users\\Public\\ClaudeFrames\\d1_watch.py
Токен бота — одной строкой в C:\\Users\\Public\\ClaudeFrames\\tg_token.txt
В GitHub Actions токен и чат берутся из секретов TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID.

⚠️ ВХОД ПО ЗАКРЫТИЮ ДНЯ = 00:00 UTC = 03:00 МСК.
   Бэктест считает вход именно по этой цене. Если входить утром, результат будет
   другим. Поэтому сканер проверяет свежесть входа: ушла цена дальше чем на 0.1% —
   помечает сигнал как просроченный (правило «не догонять» из скила).
"""
import json, time, os, sys, calendar, urllib.request, urllib.parse, datetime, traceback, statistics

BASE = os.path.dirname(os.path.abspath(__file__))
CHAT_ID = "1761629343"

# --- корзина модуля: РОВНО эти 8, расширять нельзя (проверено 08.08) ---
COINS = ["BTC", "ETH", "SOL", "BNB", "XRP", "DOGE", "ADA", "AVAX"]

# --- параметры модуля, менять нельзя без прогона через станок ---
Z_OKNO      = 50        # окно z-score
PCT_OKNO    = 60        # окно перцентилей
PCT_VERH    = 0.90      # пробой вверх -> лонг
PCT_NIZ     = 0.10      # пробой вниз  -> шорт
ATR_OKNO    = 14
SL_MULT     = 1.5       # SL = вход -+ 1.5*ATR
RR          = 2.0       # TP = 2R
TAIM_STOP   = 30        # баров D1
VOL_POROG   = 0.0168    # волатильность BTC за 20 дней
DISP_POROG  = 0.0439    # дисперсия корзины за 7 дней

# --- издержки Upscale ---
KOM_RT      = 0.00016   # комиссия туда-обратно от номинала
SPRED       = 0.00008
FAND_8H     = 0.0003    # каждые 8 часов, в обе стороны, ставка фиксированная
FAND_DNEY   = 6         # медианное удержание модуля -> 18 начислений
IZD_POROG   = 0.20      # > 0.20R -> пропуск

MAX_POZICIY = 1         # риск-правило скила: не более 1 крипто-позиции одновременно
STATE_F     = os.path.join(BASE, "d1_state.json")
LOG_F       = os.path.join(BASE, "d1_signals.jsonl")
HB_F        = os.path.join(BASE, "d1_watch.log")
ALIVE_F     = os.path.join(BASE, "d1_alive.txt")


# ------------------------------------------------------------------ утилиты
def utc_seychas():
    """Наивное UTC-время. Отдельной функцией, чтобы не сыпать предупреждениями
       в d1_err.txt — этот файл нужен для настоящих ошибок, а не для шума."""
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)


def utc_iz_ts(ts):
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).replace(tzinfo=None)


def hb(msg):
    s = "%s  %s" % (datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg)
    print(s, flush=True)
    try:
        with open(HB_F, "a", encoding="utf-8") as f:
            f.write(s + "\n")
    except Exception:
        pass


def alive():
    try:
        with open(ALIVE_F, "w") as f:
            f.write(str(int(time.time())))
    except Exception:
        pass


def http(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    for _ in range(3):
        try:
            return json.loads(urllib.request.urlopen(req, timeout=15).read().decode())
        except Exception:
            time.sleep(2)
    return None


def svechi_d1(moneta, limit=200):
    """Дневные свечи OKX. Возвращает ТОЛЬКО закрытые бары: [ts,o,h,l,c]."""
    d = http("https://www.okx.com/api/v5/market/candles?instId=%s-USDT&bar=1Dutc&limit=%d"
             % (moneta, limit))
    if not d or d.get("code") != "0":
        return []
    out = []
    for r in d["data"][::-1]:
        if r[8] != "1":            # незакрытый бар — выбросить, иначе look-ahead
            continue
        out.append([int(r[0]) // 1000, float(r[1]), float(r[2]), float(r[3]), float(r[4])])
    return out


def cena_seychas(moneta):
    d = http("https://www.okx.com/api/v5/market/ticker?instId=%s-USDT" % moneta)
    try:
        return float(d["data"][0]["last"])
    except Exception:
        return None


def tg_send(text):
    """Токен ищется в трёх местах по порядку:
       1. TELEGRAM_BOT_TOKEN — так называется секрет в репозитории с orb_scanner,
          берём то же имя, чтобы не заводить второй секрет на то же самое;
       2. TG_TOKEN — запасное имя;
       3. файл tg_token.txt — путь для запуска на Windows."""
    tok = (os.environ.get("TELEGRAM_BOT_TOKEN", "")
           or os.environ.get("TG_TOKEN", "")).strip()
    if not tok:
        try:
            tok = open(os.path.join(BASE, "tg_token.txt")).read().strip()
        except Exception:
            hb("НЕТ ТОКЕНА: ни TELEGRAM_BOT_TOKEN, ни TG_TOKEN, ни файла tg_token.txt")
            return False
    chat = (os.environ.get("TELEGRAM_CHAT_ID", "")
            or os.environ.get("TG_CHAT_ID", "")).strip() or CHAT_ID
    url = "https://api.telegram.org/bot" + tok + "/sendMessage"
    data = urllib.parse.urlencode({"chat_id": chat, "text": text,
                                   "parse_mode": "HTML"}).encode()
    try:
        urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=15)
        return True
    except Exception:
        hb("Telegram не принял сообщение")
        return False


def load_state():
    try:
        return json.load(open(STATE_F, encoding="utf-8"))
    except Exception:
        return {"otkrytye": {}, "posledniy_den": 0}


def save_state(s):
    json.dump(s, open(STATE_F, "w", encoding="utf-8"), ensure_ascii=False)


# ------------------------------------------------------------------ расчёты
def atr_arr(b, w=ATR_OKNO):
    n = len(b); out = [0.0] * n; s = 0.0
    for i in range(1, n):
        tr = max(b[i][2] - b[i][3], abs(b[i][2] - b[i-1][4]), abs(b[i][3] - b[i-1][4]))
        s += tr
        if i > w:
            j = i - w
            s -= max(b[j][2] - b[j][3], abs(b[j][2] - b[j-1][4]), abs(b[j][3] - b[j-1][4]))
        out[i] = s / min(i, w)
    return out


def zs(P, i, w=Z_OKNO):
    s = P[i-w+1:i+1]; m = sum(s) / w
    sd = (sum((y - m) ** 2 for y in s) / w) ** .5
    return (P[i] - m) / sd if sd else 0.0


def pctl(seg, q):
    s = sorted(seg); idx = q * (len(s) - 1)
    lo = int(idx); hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (idx - lo)


def rezhim(D):
    """Режим по ЗАВЕРШЁННЫМ дням. Для сигнала на последнем закрытом дне
       режим считается по данным ДО него — как в бэктесте."""
    dni = sorted(set.intersection(*[set(x[0] for x in v) for v in D.values()]))
    if len(dni) < 30:
        return None, None, None
    po = {c: {x[0]: x[4] for x in D[c]} for c in D}
    # индекс предпоследнего общего дня = день ДО сигнального
    i = len(dni) - 2
    btc = [po["BTC"][d] for d in dni]
    vol = statistics.mean(abs(btc[j] / btc[j-1] - 1) for j in range(i - 19, i + 1))
    disp = statistics.pstdev([po[c][dni[i]] / po[c][dni[i-7]] - 1 for c in D])
    return vol, disp, dni[i]


def signal_po_monete(b):
    """Сигнал на ПОСЛЕДНЕМ закрытом дневном баре. Возвращает (napravlenie, i) или None."""
    n = len(b)
    if n < Z_OKNO + PCT_OKNO + 5:
        return None
    Cc = [r[4] for r in b]
    osc = [0.0] * n
    for i in range(Z_OKNO, n):
        osc[i] = zs(Cc, i)
    i = n - 1                                  # последний закрытый бар
    seg = osc[i - PCT_OKNO:i]
    verh = pctl(seg, PCT_VERH); niz = pctl(seg, PCT_NIZ)
    if osc[i] > verh and osc[i-1] <= verh:
        return (1, i)
    if osc[i] < niz and osc[i-1] >= niz:
        return (-1, i)
    return None


def izderzhki_R(vhod, risk):
    """Полные издержки в долях R: комиссия + спред + фандинг за ожидаемое удержание."""
    nachisleniy = FAND_DNEY * 3
    dolya = KOM_RT + SPRED + FAND_8H * nachisleniy
    return vhod * dolya / risk


# ------------------------------------------------------------------ ведение позиций
def proverit_otkrytye(state, D):
    """Каждый день проверяем открытые сделки по новому дневному бару."""
    soobshcheniya = []
    for c in list(state["otkrytye"].keys()):
        p = state["otkrytye"][c]
        b = D.get(c)
        if not b:
            continue
        novye = [x for x in b if x[0] > p["ts_vhoda"]]
        if not novye:
            continue
        for bar in novye:
            if bar[0] <= p.get("ts_proveren", 0):
                continue
            p["ts_proveren"] = bar[0]
            p["barov"] = p.get("barov", 0) + 1
            d = p["napravlenie"]
            vyhod = None; prichina = ""
            if d > 0:
                if bar[3] <= p["sl"]:                       # SL приоритетнее TP
                    vyhod = p["sl"]; prichina = "СТОП"
                elif bar[2] >= p["tp"]:
                    vyhod = p["tp"]; prichina = "ТЕЙК"
            else:
                if bar[2] >= p["sl"]:
                    vyhod = p["sl"]; prichina = "СТОП"
                elif bar[3] <= p["tp"]:
                    vyhod = p["tp"]; prichina = "ТЕЙК"
            if vyhod is None and p["barov"] >= TAIM_STOP:
                vyhod = bar[4]; prichina = "ТАЙМ-СТОП %d дней" % TAIM_STOP
            if vyhod is not None:
                risk = abs(p["vhod"] - p["sl"])
                valovy = d * (vyhod - p["vhod"]) / risk
                netto = valovy - izderzhki_R(p["vhod"], risk)
                znak = "🟢" if netto > 0 else "🔴"
                soobshcheniya.append(
                    "%s <b>ВЫХОД %s %s</b>\n"
                    "причина: %s\n"
                    "вход %.6g -> выход %.6g\n"
                    "валовый %+.2fR, нетто <b>%+.2fR</b>\n"
                    "дней в сделке: %d\n\n"
                    "Записать в журнал: скажи Клоду «запиши сделку»"
                    % (znak, c, "ЛОНГ" if d > 0 else "ШОРТ", prichina,
                       p["vhod"], vyhod, valovy, netto, p["barov"]))
                try:
                    with open(LOG_F, "a", encoding="utf-8") as f:
                        f.write(json.dumps({"tip": "vyhod", "moneta": c, "prichina": prichina,
                                            "vhod": p["vhod"], "vyhod": vyhod, "netto_r": netto,
                                            "dney": p["barov"],
                                            "vremya": utc_seychas().isoformat()},
                                           ensure_ascii=False) + "\n")
                except Exception:
                    pass
                del state["otkrytye"][c]
                break
    return soobshcheniya


# ------------------------------------------------------------------ основной скан
def skan(state):
    # дешёвая проверка ДО закачки: этот день уже обработан?
    if state.get("posledniy_den", 0) >= zhdem_den():
        return False
    D = {}
    for c in COINS:
        b = svechi_d1(c)
        if len(b) < Z_OKNO + PCT_OKNO + 5:
            hb("мало данных по %s (%d баров)" % (c, len(b)))
            return False
        D[c] = b
        time.sleep(0.15)

    posl_den = D["BTC"][-1][0]
    if posl_den <= state.get("posledniy_den", 0):
        return False                                  # этот день уже обработан

    soobshcheniya = proverit_otkrytye(state, D)

    vol, disp, den_rezhima = rezhim(D)
    if vol is None:
        hb("не хватает общих дней для режима")
        return False
    rezhim_ok = (vol < VOL_POROG) and (disp < DISP_POROG)
    data_str = utc_iz_ts(posl_den).strftime("%Y-%m-%d")

    kandidaty = []
    for c in COINS:
        if c in state["otkrytye"]:
            continue                                   # позиция ещё открыта
        s = signal_po_monete(D[c])
        if s:
            kandidaty.append((c, s[0], s[1]))

    # ---------- сообщение о режиме и сигналах ----------
    if not rezhim_ok:
        if kandidaty:
            spisok = ", ".join("%s %s" % (c, "лонг" if d > 0 else "шорт") for c, d, _ in kandidaty)
            soobshcheniya.append(
                "⛔ <b>РЕЖИМ ЗАКРЫТ</b> — %s\n"
                "волатильность BTC %.2f%% (порог %.2f%%) %s\n"
                "дисперсия корзины %.2f%% (порог %.2f%%) %s\n\n"
                "Сигналы были, но модуль не торгует: %s\n"
                "Это нормально — вне режима система стоит примерно половину времени."
                % (data_str, vol * 100, VOL_POROG * 100, "✅" if vol < VOL_POROG else "❌",
                   disp * 100, DISP_POROG * 100, "✅" if disp < DISP_POROG else "❌", spisok))
    else:
        for c, d, i in kandidaty:
            b = D[c]
            A = atr_arr(b)
            a = A[i]
            if a <= 0:
                continue
            vhod = b[i][4]
            risk = SL_MULT * a
            sl = vhod - d * risk
            tp = vhod + d * RR * risk
            izd = izderzhki_R(vhod, risk)
            tek = cena_seychas(c)
            svezh = ""
            if tek:
                ushla = d * (tek - vhod) / vhod
                if ushla > 0.001:
                    svezh = "\n⚠️ ПРОСРОЧЕН: цена ушла на %.2f%% дальше входа — не догонять" % (ushla * 100)
                else:
                    svezh = "\nсейчас %.6g (отклонение %+.2f%%)" % (tek, (tek / vhod - 1) * 100)
            verdikt = "🔴 ПРОПУСК — издержки %.2fR выше порога %.2fR" % (izd, IZD_POROG) \
                      if izd > IZD_POROG else "✅ издержки в норме"
            # риск-правило против допущения бэктеста — назвать вслух, а не решать за пользователя
            uzhe = [k for k in state["otkrytye"] if k != c]
            konflikt = ""
            if len(uzhe) >= MAX_POZICIY:
                konflikt = ("\n\n⚠️ УЖЕ ОТКРЫТО: %s\n"
                            "Риск-правило скила: не более %d крипто-позиции. "
                            "Бэктест +0.447R считался БЕЗ этого ограничения — он допускал "
                            "несколько монет сразу. Возьмёшь вторую — отойдёшь от риск-правила; "
                            "пропустишь — отойдёшь от бэктеста.\n"
                            "Что бы ни выбрал, ЗАПИШИ решение в журнал, иначе форвард "
                            "будет статистикой про выбор, а не про модуль."
                            % (", ".join(uzhe), MAX_POZICIY))
            elif ("BTC" in uzhe and c == "ETH") or ("ETH" in uzhe and c == "BTC"):
                konflikt = "\n\n⚠️ BTC и ETH одновременно не торговать — правило корреляции."
            soobshcheniya.append(
                "📈 <b>МОМЕНТУМ D1 — %s %s</b>  %s\n\n"
                "вход (закрытие дня) <b>%.6g</b>\n"
                "стоп %.6g   цель %.6g\n"
                "риск %.2f%% цены, R:R 1:2, тайм-стоп %d дней\n"
                "издержки %.3fR (из них фандинг за ~%d дней)\n"
                "%s%s\n\n"
                "режим: волатильность %.2f%% ✅  дисперсия %.2f%% ✅\n"
                "риск на сделку до 1%% (модуль доказан)%s"
                % (c, "ЛОНГ" if d > 0 else "ШОРТ", data_str, vhod, sl, tp,
                   risk / vhod * 100, TAIM_STOP, izd, FAND_DNEY, verdikt, svezh,
                   vol * 100, disp * 100, konflikt))
            state["otkrytye"][c] = {"napravlenie": d, "vhod": vhod, "sl": sl, "tp": tp,
                                    "ts_vhoda": b[i][0], "ts_proveren": b[i][0], "barov": 0}
            try:
                with open(LOG_F, "a", encoding="utf-8") as f:
                    f.write(json.dumps({"tip": "signal", "moneta": c,
                                        "napravlenie": "long" if d > 0 else "short",
                                        "vhod": vhod, "sl": sl, "tp": tp, "izd_r": izd,
                                        "vol": vol, "disp": disp, "den": data_str},
                                       ensure_ascii=False) + "\n")
            except Exception:
                pass

    # ЕЖЕДНЕВНАЯ ОТМЕТКА О ЖИЗНИ.
    # Модуль даёт 4-5 сделок в МЕСЯЦ на всю корзину, то есть молчит почти каждый день.
    # Без этой строки нельзя отличить «работает и молчит» от «упал ещё в понедельник».
    if not soobshcheniya:
        otkr = ", ".join(state["otkrytye"].keys()) or "нет"
        soobshcheniya.append(
            "✅ <b>%s — сигналов нет</b>\n"
            "режим: волатильность %.2f%% (порог %.2f%%) %s, дисперсия %.2f%% (порог %.2f%%) %s\n"
            "открытые позиции: %s\n"
            "<i>сканер жив, следующая проверка завтра в 03:00 МСК</i>"
            % (data_str, vol * 100, VOL_POROG * 100, "✅" if vol < VOL_POROG else "❌",
               disp * 100, DISP_POROG * 100, "✅" if disp < DISP_POROG else "❌", otkr))
        hb("%s: сигналов нет (режим %s)" % (data_str, "открыт" if rezhim_ok else "закрыт"))
    for m in soobshcheniya:
        tg_send(m)
        time.sleep(1)

    state["posledniy_den"] = posl_den
    save_state(state)
    return True


def zhdem_den():
    """ts дневного бара, который должен был закрыться к текущему моменту.

    ⚠️ Считать строго через calendar.timegm: datetime.timestamp() трактует
    наивную дату как МЕСТНОЕ время, и на машине в UTC+3 день съезжает."""
    now = utc_seychas()
    polnoch = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return calendar.timegm(polnoch.timetuple()) - 86400


def pauza_do_skana(state):
    """Сон привязан к полуночи UTC = 03:00 МСК, когда закрывается дневной бар.

    Окно 00:00-00:20 UTC нужно НЕ для повторных сканов, а для повторных ПОПЫТОК:
    если в 03:00 отвалилась сеть или OKX ещё не пометил бар закрытым, будет
    ещё 19 заходов. Как только день обработан — сразу спим до следующей полуночи,
    лишних закачек не делаем."""
    now = utc_seychas()
    gotovo = state.get("posledniy_den", 0) >= zhdem_den()
    if now.hour == 0 and now.minute < 20 and not gotovo:
        return 60
    zavtra = (now + datetime.timedelta(days=1)).replace(
        hour=0, minute=0, second=5, microsecond=0)
    return max(60, (zavtra - now).total_seconds())


def odin_skan(zhdat_sek=2400, opros_sek=20):
    """Один заход: дождаться закрытия дневного бара и отсканировать.

    Для GitHub Actions: задача стартует в 23:50 UTC (02:50 МСК) — заранее,
    чтобы съесть задержку очереди Actions, — и ждёт появления закрытого бара.
    Скан происходит ровно тогда, когда бар закрылся, а не когда стартовала задача."""
    state = load_state()
    nado = zhdem_den()
    if state.get("posledniy_den", 0) >= nado:
        hb("день %s уже обработан — выходим"
           % utc_iz_ts(nado).strftime("%Y-%m-%d"))
        return True
    kray = time.time() + zhdat_sek
    popytka = 0
    while time.time() < kray:
        popytka += 1
        try:
            if skan(state):
                hb("скан выполнен с попытки %d" % popytka)
                return True
        except Exception:
            hb("СБОЙ: " + traceback.format_exc().strip().split("\n")[-1])
        ostalos = int(kray - time.time())
        hb("бар ещё не закрыт (попытка %d, в запасе %d мин) — ждём %d c"
           % (popytka, ostalos // 60, opros_sek))
        time.sleep(opros_sek)
    hb("не дождались закрытия бара за отведённое время")
    return False


def main():
    if "--once" in sys.argv:
        ok = odin_skan()
        sys.exit(0 if ok else 1)
    hb("D1 Watch запущен. Корзина: %s" % ", ".join(COINS))
    tg_send("🤖 <b>D1 Watch запущен</b>\n"
            "Модуль «Моментум D1 + режимный фильтр», 8 мажоров.\n"
            "Скан в 03:00 МСК, сразу после закрытия дневного бара.\n"
            "Веду открытые позиции до выхода и присылаю результат в R.")
    state = load_state()
    while True:
        alive()
        try:
            skan(state)
        except Exception:
            hb("СБОЙ: " + traceback.format_exc().strip().split("\n")[-1])
        time.sleep(pauza_do_skana(state))


if __name__ == "__main__":
    main()
