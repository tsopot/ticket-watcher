#!/usr/bin/env python3
"""
Моніторинг вільних квитків Wisła Kraków (bilety.wislakrakow.com) -> Telegram.
 
Список матчів береться з events.json. Для кожного матчу скрипт запитує
GetWGLSectorsInfo: порожній масив "sectors" = квитків немає, непорожній = є.
 
Лише стандартна бібліотека Python. Секрети - тільки зі змінних оточення.
"""
import hashlib
import http.cookiejar
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
 
BASE = "https://bilety.wislakrakow.com"
EVENTS_FILE = Path("events.json")
STATE_FILE = Path("state/state.json")
 
REMIND_EVERY_SEC = 30 * 60        # повторне нагадування, поки квитки доступні
HEARTBEAT_EVERY_SEC = 24 * 3600   # "я живий" раз на добу (без звуку)
FAIL_ALERT_AFTER = 3              # збоїв поспіль до сповіщення про проблему
FAIL_REALERT_EVERY_SEC = 6 * 3600
 
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
 
 
def load_state():
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}
 
 
def save_state(state):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state), encoding="utf-8")
 
 
def load_events():
    override = os.environ.get("EVENT_ID", "").strip()
    if override:  # разова ручна перевірка з GitHub UI
        return [{"id": override, "name": f"Подія {override}"}]
    events = json.loads(EVENTS_FILE.read_text(encoding="utf-8"))
    for ev in events:
        ev["id"] = str(ev["id"])
        ev.setdefault("name", f"Подія {ev['id']}")
    return events
 
 
def fetch(opener, url, referer=None, as_json=True):
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": (
            "application/json, text/javascript, */*; q=0.01" if as_json
            else "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
        ),
        "Accept-Language": "pl-PL,pl;q=0.9,en-US;q=0.8,en;q=0.7",
        "Cache-Control": "no-cache",
    }
    if referer:
        headers["Referer"] = referer
        headers["X-Requested-With"] = "XMLHttpRequest"
        headers["Sec-Fetch-Dest"] = "empty"
        headers["Sec-Fetch-Mode"] = "cors"
        headers["Sec-Fetch-Site"] = "same-origin"
    else:
        headers["Sec-Fetch-Dest"] = "document"
        headers["Sec-Fetch-Mode"] = "navigate"
        headers["Sec-Fetch-Site"] = "none"
        headers["Upgrade-Insecure-Requests"] = "1"
    last_err = None
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers=headers)
            with opener.open(req, timeout=20) as resp:
                raw = resp.read(5_000_000)
                status = resp.status
                ctype = resp.headers.get("Content-Type", "")
                hdrs = {
                    k: resp.headers.get(k)
                    for k in ("Server", "Content-Length", "Content-Encoding",
                              "Cache-Control", "CF-RAY", "X-Powered-By", "Via")
                    if resp.headers.get(k)
                }
            body = raw.decode("utf-8-sig", "replace")  # utf-8-sig прибирає можливий BOM
            if not as_json:
                return body
            try:
                return json.loads(body)
            except json.JSONDecodeError:
                snippet = " ".join(body.split())[:200]
                last_err = f"не JSON (HTTP {status}, {ctype}, {len(raw)} байт, {hdrs}): {snippet!r}"
        except urllib.error.HTTPError as e:
            if e.code in (401, 403, 429):
                raise RuntimeError(f"HTTP {e.code} (можливе блокування)") from None
            last_err = f"HTTP {e.code}"
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last_err = type(e).__name__
        time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"запит не вдався: {last_err}")
 
 
def send_telegram(token, chat_id, text, silent=False):
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = urllib.parse.urlencode({
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": "true",
        "disable_notification": "true" if silent else "false",
    }).encode()
    try:
        req = urllib.request.Request(url, data=data, method="POST")
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.status == 200
    except urllib.error.HTTPError as e:
        print(f"Telegram: HTTP {e.code}")
    except Exception as e:  # без деталей, щоб не світити URL з токеном
        print(f"Telegram: помилка {type(e).__name__}")
    return False
 
 
def sector_labels(sectors, names):
    labels = []
    for item in sectors:
        if isinstance(item, dict):
            sid = item.get("id", item.get("sectorId"))
            name = item.get("name") or names.get(sid) or sid
        else:
            name = names.get(item, item)
        labels.append(str(name if name is not None else "?"))
    return labels
 
 
def free_count(item):
    """К-сть вільних місць у секторі, або None, якщо формат невідомий."""
    if isinstance(item, dict):
        areas = item.get("freeSeatsByPriceArea")
        if isinstance(areas, list):
            total = 0
            for a in areas:
                if isinstance(a, dict):
                    try:
                        total += int(a.get("freeSeatsNo") or 0)
                    except (TypeError, ValueError):
                        pass
            return total
    return None
 
 
def check_event(ev, st, token, chat_id, now):
    """Перевіряє одну подію. Повертає 'free' | 'none' | 'error'."""
    eid = ev["id"]
    page_url = f"{BASE}/Stadium/Index?eventId={eid}"
    info_url = f"{BASE}/Stadium/GetWGLSectorsInfo?eventId={eid}"
    sectors_url = f"{BASE}/Stadium/GetWGLSectors?eventId={eid}"
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
 
    try:
        try:
            page = fetch(opener, page_url, as_json=False)  # cookies, як у браузера
            print(f"[{eid}] сторінка: {len(page)} символів; cookies: {[c.name for c in jar]}")
        except Exception as e:
            print(f"[{eid}] сторінка події (cookies): {e}")
        info = fetch(opener, info_url, referer=page_url)
        sectors = info.get("sectors") if isinstance(info, dict) else None
        if not isinstance(sectors, list):
            raise RuntimeError("неочікуваний формат відповіді (немає масиву 'sectors')")
    except Exception as e:
        fails = int(st.get("fail_count", 0)) + 1
        st["fail_count"] = fails
        print(f"[{eid}] збій #{fails}: {e}")
        if fails >= FAIL_ALERT_AFTER and now - float(st.get("last_fail_alert", 0)) > FAIL_REALERT_EVERY_SEC:
            if send_telegram(
                token, chat_id,
                f"⚠️ {ev['name']}: проблема з моніторингом ({fails} збоїв поспіль): {e}\n{page_url}",
            ):
                st["last_fail_alert"] = now
        return "error"
 
    if int(st.get("fail_count", 0)) >= FAIL_ALERT_AFTER:
        send_telegram(token, chat_id, f"✅ {ev['name']}: моніторинг знову працює.", silent=True)
    st["fail_count"] = 0
 
    # Якщо формат відомий (freeSeatsByPriceArea) - рахуємо реальні вільні місця,
    # сектори з 0 місць ігноруємо. Невідомий формат: будь-який елемент = є квитки.
    counts = [free_count(x) for x in sectors]
    if sectors and all(c is not None for c in counts):
        kept = [(x, c) for x, c in zip(sectors, counts) if c > 0]
        sectors = [x for x, _ in kept]
        counts = [c for _, c in kept]
    available = len(sectors) > 0
    print(f"[{eid}] {datetime.now(timezone.utc).isoformat()} sectors={len(sectors)} available={available}")
 
    if not available:
        st["last_sig"] = ""  # щоб наступна поява квитків знову дала сповіщення
        return "none"
 
    try:
        names_raw = fetch(opener, sectors_url, referer=page_url)
        names = {s.get("id"): s.get("name") for s in names_raw.get("sectors", [])}
    except Exception:
        names = {}
    labels = sector_labels(sectors, names)
    sig = hashlib.sha1("|".join(sorted(labels)).encode()).hexdigest()
    changed = sig != st.get("last_sig")
    due = now - float(st.get("last_notified", 0)) > REMIND_EVERY_SEC
    if changed or due:
        raw = json.dumps(sectors, ensure_ascii=False)[:400]
        shown = [f"{l} ({c})" if c is not None else l for l, c in zip(labels, counts)]
        total = (
            f"Всього вільних місць: {sum(counts)}\n"
            if all(c is not None for c in counts) else ""
        )
        text = (
            f"🎟 З'явилися квитки: {ev['name']}\n"
            f"Сектори: {', '.join(shown)}\n"
            f"{total}\n"
            f"{page_url}\n\n"
            f"(дані: {raw})"
        )
        if send_telegram(token, chat_id, text):
            st["last_sig"] = sig
            st["last_notified"] = now
    return "free"
 
 
def main():
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        print("Не задано TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID")
        return 1
 
    if os.environ.get("TEST_MODE") == "1":
        ok = send_telegram(token, chat_id, "✅ Тест: бот налаштований правильно.")
        print("Тестове повідомлення:", "надіслано" if ok else "НЕ надіслано")
        return 0 if ok else 1
 
    now = time.time()
    now_dt = datetime.now(timezone.utc)
    events = []
    for ev in load_events():
        dl = ev.get("deadline")
        if dl and now_dt > datetime.fromisoformat(dl.replace("Z", "+00:00")):
            print(f"[{ev['id']}] подія вже відбулась, пропускаю")
            continue
        events.append(ev)
    if not events:
        print("Немає активних подій для моніторингу.")
        return 0
 
    state = load_state()
    ev_state = state.setdefault("events", {})
    results = {}
    for ev in events:
        st = ev_state.setdefault(ev["id"], {})
        results[ev["id"]] = check_event(ev, st, token, chat_id, now)
        time.sleep(1)
 
    # Щодобове "я живий": лише якщо всі перевірки пройшли без помилок і квитків ще немає
    all_ok = all(r != "error" for r in results.values())
    any_free = any(r == "free" for r in results.values())
    if all_ok and not any_free and now - float(state.get("last_heartbeat", 0)) > HEARTBEAT_EVERY_SEC:
        names = "; ".join(ev["name"] for ev in events)
        if send_telegram(
            token, chat_id,
            f"💤 Моніторинг працює. Вільних квитків поки немає.\nСлідкую за: {names}",
            silent=True,
        ):
            state["last_heartbeat"] = now
 
    save_state(state)
    return 0
 
 
if __name__ == "__main__":
    sys.exit(main())
 
