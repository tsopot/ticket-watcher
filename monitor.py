#!/usr/bin/env python3
"""
Моніторинг вільних квитків Wisła Kraków (bilety.wislakrakow.com) -> Telegram.
 
Сторінка події відкривається у справжньому (headless) браузері Chromium через Playwright:
сайт сам виконує свій JavaScript і сам робить запити до API. Ми лише читаємо відповідь
GetWGLSectorsInfo, яку отримала сторінка. Жодних підроблених токенів чи заголовків.
 
Список матчів - events.json. Порожній масив "sectors" = квитків немає.
Секрети беруться ТІЛЬКИ зі змінних оточення.
"""
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse
 
BASE = "https://bilety.wislakrakow.com"
EVENTS_FILE = Path("events.json")
STATE_FILE = Path("state/state.json")
 
NAV_TIMEOUT_MS = 45_000
API_WAIT_SEC = 30
 
REMIND_EVERY_SEC = 30 * 60        # повторне нагадування, поки квитки доступні
HEARTBEAT_EVERY_SEC = 24 * 3600   # "я живий" раз на добу (без звуку)
FAIL_ALERT_AFTER = 3              # збоїв поспіль до сповіщення про проблему
FAIL_REALERT_EVERY_SEC = 6 * 3600
 
 
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
 
 
def parse_json_body(text, what):
    text = (text or "").lstrip("\ufeff")
    if not text.strip():
        raise RuntimeError(f"{what}: порожня відповідь")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        snippet = " ".join(text.split())[:200]
        raise RuntimeError(f"{what}: відповідь не є JSON: {snippet!r}") from None
 
 
def load_event_data(browser, page_url):
    """Відкриває сторінку події як звичайний браузер.
    Повертає (info, names): відповідь GetWGLSectorsInfo і {id сектора: назва}."""
    ctx = browser.new_context(
        locale="pl-PL",
        timezone_id="Europe/Warsaw",
        viewport={"width": 1366, "height": 900},
    )
    try:
        page = ctx.new_page()
        # Не вантажимо картинки/шрифти/медіа: швидше і менше навантаження на сайт
        page.route(
            "**/*",
            lambda route: route.abort()
            if route.request.resource_type in ("image", "media", "font")
            else route.continue_(),
        )
        seen = {}
 
        def on_response(resp):
            name = urlparse(resp.url).path.rsplit("/", 1)[-1]
            if name in ("GetWGLSectorsInfo", "GetWGLSectors"):
                seen[name] = resp
 
        page.on("response", on_response)
        page.goto(page_url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
 
        deadline = time.time() + API_WAIT_SEC
        while "GetWGLSectorsInfo" not in seen and time.time() < deadline:
            page.wait_for_timeout(500)
        resp = seen.get("GetWGLSectorsInfo")
        if resp is None:
            raise RuntimeError(
                "сторінка не зробила запит GetWGLSectorsInfo (змінилась структура сайту або захист?)"
            )
        page.wait_for_timeout(1500)  # дати час на GetWGLSectors (назви секторів)
 
        if resp.status != 200:
            raise RuntimeError(f"GetWGLSectorsInfo: HTTP {resp.status}")
        info = parse_json_body(resp.text(), "GetWGLSectorsInfo")
 
        names = {}
        sec = seen.get("GetWGLSectors")
        if sec is not None and sec.status == 200:
            try:
                data = parse_json_body(sec.text(), "GetWGLSectors")
                names = {s.get("id"): s.get("name") for s in data.get("sectors", [])}
            except Exception:
                names = {}
        return info, names
    finally:
        ctx.close()
 
 
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
 
 
def check_event(ev, st, token, chat_id, now, browser):
    """Перевіряє одну подію. Повертає 'free' | 'none' | 'error'."""
    eid = ev["id"]
    page_url = f"{BASE}/Stadium/Index?eventId={eid}"
 
    try:
        info, names = load_event_data(browser, page_url)
        sectors = info.get("sectors") if isinstance(info, dict) else None
        if not isinstance(sectors, list):
            raise RuntimeError("неочікуваний формат відповіді (немає масиву 'sectors')")
    except Exception as e:
        fails = int(st.get("fail_count", 0)) + 1
        st["fail_count"] = fails
        msg = f"{type(e).__name__}: {e}" if not isinstance(e, RuntimeError) else str(e)
        msg = " ".join(msg.split())[:300]
        print(f"[{eid}] збій #{fails}: {msg}")
        if fails >= FAIL_ALERT_AFTER and now - float(st.get("last_fail_alert", 0)) > FAIL_REALERT_EVERY_SEC:
            if send_telegram(
                token, chat_id,
                f"⚠️ {ev['name']}: проблема з моніторингом ({fails} збоїв поспіль): {msg}\n{page_url}",
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
 
    from playwright.sync_api import sync_playwright
 
    state = load_state()
    ev_state = state.setdefault("events", {})
    results = {}
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            for ev in events:
                st = ev_state.setdefault(ev["id"], {})
                results[ev["id"]] = check_event(ev, st, token, chat_id, now, browser)
                time.sleep(1)
        finally:
            browser.close()
 
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
 
