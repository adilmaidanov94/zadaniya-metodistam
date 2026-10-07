#!/usr/bin/env python3
"""Локальный сервер «Задания методистам».

Запуск:  python3 server.py
Без внешних зависимостей (только стандартная библиотека Python 3).
Настройки через переменные окружения:
  PORT     — порт (по умолчанию 8080)
  ZAM_PIN  — PIN-код замдекана (по умолчанию 1234)
Telegram-бот для уведомлений настраивается на сайте: «Настройки» у замдекана.
"""
import copy
import datetime
import html
import json
import mimetypes
import os
import queue
import re
import secrets
import socket
import ssl
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(ROOT, "data")
DATA_FILE = os.path.join(DATA_DIR, "tasks.json")
CONFIG_FILE = os.path.join(DATA_DIR, "config.json")
STATIC_DIR = os.path.join(ROOT, "static")
INDEX_FILE = os.path.join(STATIC_DIR, "index.html")
AVATAR_DIR = os.path.join(STATIC_DIR, "avatars")
IMAGE_EXT = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".svg")
ONLINE_SECONDS = 15

PORT = int(os.environ.get("PORT", "8080"))
PIN = os.environ.get("ZAM_PIN", "1234")
TELEGRAM_API = os.environ.get("TELEGRAM_API", "https://api.telegram.org")
REMIND_HOURS = (8, 21)  # напоминания о сроках только с 8:00 до 21:00

METHODISTS = ["Эльмира", "Улзира", "Перизат"]
STATUSES = ["new", "progress", "review", "done"]
STATUS_RU = {"new": "Новое", "progress": "В работе", "review": "На проверке", "done": "Выполнено"}
PRIORITIES = ["low", "normal", "high"]
PRIORITY_RU = {"low": "Низкий", "normal": "Средний", "high": "Высокий"}
MONTHS_GEN = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа",
              "сентября", "октября", "ноября", "декабря"]
ADMIN_NAME = "Замдекана"
PEOPLE = METHODISTS + ["admin"]  # кому можно слать уведомления

lock = threading.Lock()
PRESENCE = {}  # кто открыл доску: имя -> время последнего опроса
PUBLIC_URL = f"http://localhost:{PORT}"


def now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def read_json(path, default):
    if not os.path.exists(path):
        return default
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def write_json(path, obj):
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def save(state):
    write_json(DATA_FILE, state)


STATE = read_json(DATA_FILE, {"version": 1, "tasks": []})
STATE.setdefault("notifications", [])
CONFIG = read_json(CONFIG_FILE, {})
CONFIG.setdefault("telegram", {})


def tg_conf():
    tg = CONFIG["telegram"]
    tg.setdefault("chats", {})
    tg.setdefault("codes", {})
    return tg


def who(auth):
    """Возвращает 'admin', имя методиста или None."""
    if not isinstance(auth, dict):
        return None
    role = auth.get("role")
    if role == "admin":
        return "admin" if str(auth.get("pin", "")) == PIN else None
    if role in METHODISTS:
        return role
    return None


def clean_date(v):
    v = str(v or "")
    return v if re.fullmatch(r"\d{4}-\d{2}-\d{2}", v) else ""


def fmt_date(s):
    y, m, d = map(int, s.split("-"))
    return f"{d} {MONTHS_GEN[m - 1]}" + ("" if y == datetime.date.today().year else f" {y}")


def clamp_progress(v):
    try:
        return max(0, min(100, int(v)))
    except (TypeError, ValueError):
        return None


def column(assignee, exclude=None):
    return sorted(
        (t for t in STATE["tasks"] if t.get("assignee") == assignee and t is not exclude),
        key=lambda t: t.get("order", 0),
    )


def log(task, by, text):
    task.setdefault("log", []).append({"at": now(), "by": by, "text": text})
    task["log"] = task["log"][-60:]


def set_status(task, status, by):
    if status == task["status"]:
        return
    task["status"] = status
    if status == "done":
        task["progress"] = 100
        task["done_at"] = now()
    else:
        task["done_at"] = None
    log(task, by, f"Статус: {STATUS_RU[status]}")


def set_assignee(task, assignee, by):
    if task.get("assignee") == assignee:
        return
    task["assignee"] = assignee
    task["assigned_at"] = now() if assignee else None
    log(task, by, f"Назначено: {assignee}" if assignee else "Возвращено в банк заданий")


def update_task(task, body, user):
    by = ADMIN_NAME if user == "admin" else user
    old_status = task["status"]
    old_progress = task["progress"]

    # Сначала все проверки, чтобы отказ не оставлял задание изменённым наполовину
    if user == "admin":
        allowed_statuses = STATUSES
        if "title" in body and not str(body["title"]).strip():
            raise ValueError("Название не может быть пустым")
    else:
        if task.get("assignee") != user:
            raise PermissionError("Это задание назначено другому методисту")
        if task["status"] == "done":
            raise PermissionError("Задание уже принято замдекана")
        allowed_statuses = ["new", "progress", "review"]
    new_status = body.get("status", old_status)
    if new_status not in allowed_statuses:
        raise PermissionError("Этот статус недоступен")

    if user == "admin":
        if "title" in body:
            task["title"] = str(body["title"]).strip()[:300]
        if "desc" in body:
            task["desc"] = str(body["desc"])[:5000]
        if "deadline" in body:
            task["deadline"] = clean_date(body["deadline"])
        if body.get("priority") in PRIORITIES:
            task["priority"] = body["priority"]
        if "assignee" in body:
            target = body["assignee"] if body["assignee"] in METHODISTS else None
            if target != task.get("assignee"):
                rest = column(target, exclude=task)
                task["order"] = (rest[-1]["order"] + 1) if rest else 0
                set_assignee(task, target, by)

    if "note" in body:
        note = str(body["note"])[:3000]
        if note != task.get("note", ""):
            task["note"] = note
            log(task, by, "Обновлён комментарий")

    if "progress" in body:
        pr = clamp_progress(body["progress"])
        if pr is not None and pr != old_progress:
            task["progress"] = pr
            log(task, by, f"Прогресс: {pr}%")
            # Автоматическая связка прогресса и статуса, если статус не меняли вручную
            if new_status == old_status:
                if pr == 100 and new_status in ("new", "progress"):
                    new_status = "review"
                elif pr > 0 and new_status == "new":
                    new_status = "progress"

    set_status(task, new_status, by)
    task["updated_at"] = now()
    task["updated_by"] = by


# ---------------------------------------------------------------- уведомления

def task_details(t):
    lines = []
    if t.get("deadline"):
        lines.append(f"Срок: до {fmt_date(t['deadline'])}")
    lines.append(f"Приоритет: {PRIORITY_RU[t['priority']]}")
    if t.get("desc"):
        d = t["desc"].strip()
        lines.append(d if len(d) <= 400 else d[:400] + "…")
    return "\n".join(lines)


def task_events(before, after, user):
    """Что изменилось в задании и кому об этом сообщить: [(кому, текст, подробности)]."""
    t = after or before
    q = f"«{t['title']}»"
    a0 = before.get("assignee") if before else None
    if after is None:
        return [(a0, f"Задание {q} отменено замдекана", None)] if a0 else []
    a1 = after.get("assignee")
    ev = []
    if a1 != a0:
        if a1:
            ev.append((a1, f"Вам назначено новое задание: {q}", task_details(after)))
        if a0:
            ev.append((a0, f"Задание {q} передано: {a1}" if a1 else f"Задание {q} снято с вас", None))
        return ev
    if before is None or not a1:
        return ev
    if user == "admin":
        s0, s1 = before["status"], after["status"]
        if s1 != s0:
            if s1 == "done":
                ev.append((a1, f"Задание {q} принято. Спасибо!", None))
            elif s0 in ("review", "done"):
                ev.append((a1, f"Задание {q} возвращено на доработку", None))
            else:
                ev.append((a1, f"Статус задания {q}: {STATUS_RU[s1]}", None))
        if after["deadline"] != before["deadline"]:
            new = f"до {fmt_date(after['deadline'])}" if after["deadline"] else "без срока"
            ev.append((a1, f"Изменён срок задания {q}: {new}", None))
        if after["title"] != before["title"] or after["desc"] != before["desc"]:
            ev.append((a1, f"Изменены условия задания {q}", task_details(after)))
    else:
        note = after.get("note", "")
        if after["status"] == "review" and before["status"] != "review":
            ev.append(("admin", f"{user}: задание {q} готово, ждёт проверки",
                       f"Комментарий: {note}" if note else None))
        elif note and note != before.get("note", ""):
            ev.append(("admin", f"Новый комментарий от {user} к заданию {q}", note))
    return ev


def notify(to, text, task_id=None, details=None):
    """Уведомление внутри сайта + в Telegram, если человек подключил бота. Вызывать под lock."""
    if to not in PEOPLE:
        return
    STATE["notifications"].append({"id": uuid.uuid4().hex[:10], "to": to, "at": now(),
                                   "text": text, "task": task_id, "read": False})
    STATE["notifications"] = STATE["notifications"][-500:]
    tg = tg_conf()
    chat = tg["chats"].get(to)
    if tg.get("token") and chat:
        msg = f"<b>{html.escape(text)}</b>"
        if details:
            msg += "\n\n" + html.escape(details)
        link = PUBLIC_URL + ("/" if to == "admin" else "/?as=" + to)
        msg += f"\n\nОткрыть доску: {link}"
        TG_QUEUE.put((chat, msg))


def emit(before, after, user):
    task_id = (after or before)["id"]
    for to, text, details in task_events(before, after, user):
        notify(to, text, task_id if after else None, details)


def check_deadlines():
    """Напоминания: за день до срока, в день срока и при просрочке. Вызывать под lock."""
    today = datetime.date.today()
    changed = False
    for t in STATE["tasks"]:
        a = t.get("assignee")
        if not a or not t.get("deadline") or t["status"] in ("done", "review"):
            continue
        rem = t.setdefault("reminded", {})
        if rem.get("for") != t["deadline"]:
            rem.clear()
            rem["for"] = t["deadline"]
        d = datetime.date.fromisoformat(t["deadline"])
        q = f"«{t['title']}»"
        if d == today + datetime.timedelta(days=1) and not rem.get("d1"):
            notify(a, f"Напоминание: завтра срок задания {q}", t["id"], f"Сейчас выполнено: {t['progress']}%")
            rem["d1"] = changed = True
        elif d == today and not rem.get("d0"):
            notify(a, f"Сегодня последний день по заданию {q}", t["id"], f"Сейчас выполнено: {t['progress']}%")
            rem["d0"] = changed = True
        elif d < today and not rem.get("over"):
            notify(a, f"Срок задания {q} прошёл ({fmt_date(t['deadline'])})", t["id"],
                   "Обновите статус или напишите комментарий для замдекана.")
            notify("admin", f"Просрочено: {q} — {a}, срок был {fmt_date(t['deadline'])}", t["id"],
                   f"Выполнено: {t['progress']}%")
            rem["over"] = changed = True
    return changed


def reminder_loop():
    while True:
        if REMIND_HOURS[0] <= time.localtime().tm_hour < REMIND_HOURS[1]:
            with lock:
                if check_deadlines():
                    STATE["version"] += 1
                    save(STATE)
        time.sleep(300)


# ---------------------------------------------------------------- Telegram

TG_QUEUE = queue.Queue()


def ssl_context():
    ctx = ssl.create_default_context()
    # Python с python.org на Mac часто без сертификатов — берём системные
    if not ctx.cert_store_stats().get("x509_ca"):
        for path in ("/etc/ssl/cert.pem", "/etc/ssl/certs/ca-certificates.crt"):
            if os.path.exists(path):
                ctx.load_verify_locations(path)
                break
    return ctx


SSL_CTX = ssl_context()


def tg_api(token, method, params=None, timeout=20):
    url = f"{TELEGRAM_API}/bot{token}/{method}"
    data = urllib.parse.urlencode(params or {}).encode()
    req = urllib.request.Request(url, data=data)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read())
        except ValueError:
            return {"ok": False, "description": f"HTTP {e.code}"}


def tg_send(chat_id, text):
    token = tg_conf().get("token")
    if token:
        tg_api(token, "sendMessage", {"chat_id": chat_id, "text": text, "parse_mode": "HTML",
                                      "disable_web_page_preview": "true"})


def tg_sender():
    while True:
        chat, msg = TG_QUEUE.get()
        for attempt in range(3):
            try:
                tg_send(chat, msg)
                break
            except (urllib.error.URLError, OSError, ValueError) as e:
                if attempt == 2:
                    print(f"  [Telegram] не удалось отправить сообщение: {e}")
                time.sleep(5)


def person_name(key):
    return ADMIN_NAME if key == "admin" else key


def tg_handle(update):
    msg = update.get("message") or {}
    chat_id = (msg.get("chat") or {}).get("id")
    text = (msg.get("text") or "").strip()
    if not chat_id:
        return
    tg = tg_conf()
    if text.startswith("/start"):
        code = text.split(maxsplit=1)[1] if " " in text else ""
        with lock:
            key = next((k for k, c in tg["codes"].items() if c == code and code), None)
            if key:
                tg["chats"][key] = chat_id
                write_json(CONFIG_FILE, CONFIG)
        if key:
            tg_send(chat_id, f"Готово, {html.escape(person_name(key))}! Теперь уведомления о заданиях "
                             f"будут приходить сюда.\n\nОткрыть доску: {PUBLIC_URL}\nОтключить: /stop")
        else:
            tg_send(chat_id, "Чтобы получать уведомления, откройте личную ссылку для подключения: "
                             "её можно взять на сайте «Задания методистам» (колокольчик → «Получать в Telegram») "
                             "или попросить у замдекана.")
    elif text.startswith("/stop"):
        with lock:
            keys = [k for k, c in tg["chats"].items() if c == chat_id]
            for k in keys:
                del tg["chats"][k]
            write_json(CONFIG_FILE, CONFIG)
        tg_send(chat_id, "Уведомления отключены. Подключить снова можно по личной ссылке с сайта.")
    else:
        tg_send(chat_id, f"Я присылаю уведомления о заданиях. Отвечать и менять статус — на сайте: {PUBLIC_URL}")


def tg_poller():
    while True:
        token = tg_conf().get("token")
        if not token:
            time.sleep(3)
            continue
        try:
            res = tg_api(token, "getUpdates", {"offset": tg_conf().get("offset", 0), "timeout": 25}, timeout=35)
            if not res.get("ok"):
                time.sleep(10)
                continue
            for upd in res.get("result", []):
                with lock:
                    tg_conf()["offset"] = upd["update_id"] + 1
                    write_json(CONFIG_FILE, CONFIG)
                if tg_conf().get("token") == token:
                    tg_handle(upd)
        except (urllib.error.URLError, OSError, ValueError):
            time.sleep(10)


def tg_status():
    tg = tg_conf()
    return {"enabled": bool(tg.get("token")), "bot": tg.get("bot"), "linked": sorted(tg["chats"])}


def tg_link(key):
    tg = tg_conf()
    if not tg["codes"].get(key):
        tg["codes"][key] = secrets.token_urlsafe(9)
        write_json(CONFIG_FILE, CONFIG)
    return f"https://t.me/{tg['bot']}?start={tg['codes'][key]}"


# ---------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    server_version = "Zadaniya/1.0"

    def log_message(self, fmt, *args):
        pass

    def send_json(self, code, obj):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def snapshot(self, key=None):
        t = time.time()
        online = [n for n, seen in PRESENCE.items() if t - seen < ONLINE_SECONDS]
        mine = [n for n in STATE["notifications"] if key and n["to"] == key][-60:][::-1]
        return {"version": STATE["version"], "tasks": STATE["tasks"], "methodists": METHODISTS,
                "online": online, "avatars": avatars(), "logo": logo(),
                "notifications": mine, "telegram": tg_status()}

    def send_file(self, path):
        with open(path, "rb") as f:
            data = f.read()
        ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
        if ctype.startswith("text/"):
            ctype += "; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        url = urlparse(self.path)
        path = url.path
        if path in ("/", "/index.html"):
            return self.send_file(INDEX_FILE)
        if path == "/api/state":
            who_ = nfc(parse_qs(url.query).get("who", [""])[0])
            key = who_ if who_ in PEOPLE else None
            with lock:
                if key:
                    PRESENCE[key] = time.time()
                return self.send_json(200, self.snapshot(key))
        # Остальные файлы из static/ (фото, логотип); выйти за пределы папки нельзя
        full = os.path.realpath(os.path.join(STATIC_DIR, unquote(path).lstrip("/")))
        if full.startswith(os.path.realpath(STATIC_DIR) + os.sep) and os.path.isfile(full):
            return self.send_file(full)
        self.send_json(404, {"error": "Не найдено"})

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(body, dict):
                raise ValueError
        except (ValueError, json.JSONDecodeError):
            return self.send_json(400, {"error": "Некорректный запрос"})

        user = who(body.get("auth"))
        if path == "/api/login":
            if user:
                return self.send_json(200, {"ok": True})
            return self.send_json(403, {"error": "Неверный PIN-код"})
        if not user:
            return self.send_json(403, {"error": "Нет доступа. Войдите заново."})
        if path.startswith("/api/telegram/"):
            return self.telegram(path.rsplit("/", 1)[1], body, user)

        parts = path.strip("/").split("/")
        with lock:
            try:
                if parts == ["api", "notifications", "read"]:
                    ids = body.get("ids")
                    for n in STATE["notifications"]:
                        if n["to"] == user and (ids is None or n["id"] in ids):
                            n["read"] = True
                    save(STATE)
                    return self.send_json(200, {"ok": True, **self.snapshot(user)})
                if parts == ["api", "tasks"]:
                    task = self.create(body, user)
                    emit(None, task, user)
                elif len(parts) in (3, 4) and parts[:2] == ["api", "tasks"]:
                    task = next((t for t in STATE["tasks"] if t["id"] == parts[2]), None)
                    if task is None:
                        return self.send_json(404, {"error": "Задание не найдено (возможно, удалено)"})
                    action = parts[3] if len(parts) == 4 else "update"
                    before = copy.deepcopy(task)
                    if action == "update":
                        update_task(task, body, user)
                    elif action == "move":
                        self.move(task, body, user)
                    elif action == "delete":
                        if user != "admin":
                            raise PermissionError("Удалять задания может только замдекана")
                        STATE["tasks"].remove(task)
                    else:
                        return self.send_json(404, {"error": "Не найдено"})
                    emit(before, None if action == "delete" else task, user)
                else:
                    return self.send_json(404, {"error": "Не найдено"})
            except PermissionError as e:
                return self.send_json(403, {"error": str(e)})
            except ValueError as e:
                return self.send_json(400, {"error": str(e)})

            STATE["version"] += 1
            save(STATE)
            self.send_json(200, {"ok": True, "task": task, **self.snapshot(user)})

    def telegram(self, action, body, user):
        tg = tg_conf()
        if action == "config":
            if user != "admin":
                return self.send_json(403, {"error": "Настраивать бота может только замдекана"})
            token = str(body.get("token", "")).strip()
            if not token:
                with lock:
                    CONFIG["telegram"] = {}
                    write_json(CONFIG_FILE, CONFIG)
                    return self.send_json(200, {"ok": True, **self.snapshot(user)})
            if not re.fullmatch(r"\d+:[\w-]{20,}", token):
                return self.send_json(400, {"error": "Это не похоже на токен бота. Он выглядит так: 1234567890:AAH…"})
            try:
                me = tg_api(token, "getMe", timeout=15)
            except (urllib.error.URLError, OSError, ValueError) as e:
                return self.send_json(502, {"error": f"Нет связи с Telegram с этого компьютера ({e}). "
                                                     "Проверьте интернет или спросите IT, не закрыт ли Telegram."})
            if not me.get("ok"):
                return self.send_json(400, {"error": "Telegram не принял токен: " + str(me.get("description", ""))})
            with lock:
                if tg.get("token") != token:
                    CONFIG["telegram"] = {"token": token, "bot": me["result"]["username"],
                                          "chats": {}, "codes": tg.get("codes", {}), "offset": 0}
                    write_json(CONFIG_FILE, CONFIG)
                return self.send_json(200, {"ok": True, **self.snapshot(user)})

        if not tg.get("token"):
            return self.send_json(400, {"error": "Telegram-бот ещё не настроен замдекана"})
        with lock:
            if action == "link":
                return self.send_json(200, {"ok": True, "url": tg_link(user)})
            if action == "unlink":
                tg["chats"].pop(user, None)
                write_json(CONFIG_FILE, CONFIG)
                return self.send_json(200, {"ok": True, **self.snapshot(user)})
            if user != "admin":
                return self.send_json(403, {"error": "Нет доступа"})
            if action == "links":
                return self.send_json(200, {"ok": True, "links": {k: tg_link(k) for k in PEOPLE}})
            if action == "test":
                to = body.get("to")
                if to not in tg["chats"]:
                    return self.send_json(400, {"error": "Этот человек ещё не подключил Telegram"})
                TG_QUEUE.put((tg["chats"][to], f"<b>Проверка связи</b>\n\nЗдравствуйте, {html.escape(person_name(to))}! "
                                               f"Уведомления о заданиях будут приходить сюда."))
                return self.send_json(200, {"ok": True})
        return self.send_json(404, {"error": "Не найдено"})

    def create(self, body, user):
        if user != "admin":
            raise PermissionError("Создавать задания может только замдекана")
        title = str(body.get("title", "")).strip()[:300]
        if not title:
            raise ValueError("Введите название задания")
        assignee = body.get("assignee") if body.get("assignee") in METHODISTS else None
        col = column(assignee)
        task = {
            "id": uuid.uuid4().hex[:10],
            "title": title,
            "desc": str(body.get("desc", ""))[:5000],
            "deadline": clean_date(body.get("deadline")),
            "priority": body.get("priority") if body.get("priority") in PRIORITIES else "normal",
            "assignee": None,
            "status": "new",
            "progress": 0,
            "note": "",
            "order": (col[0]["order"] - 1) if col else 0,
            "created_at": now(),
            "updated_at": now(),
            "updated_by": ADMIN_NAME,
            "assigned_at": None,
            "done_at": None,
            "log": [],
        }
        log(task, ADMIN_NAME, "Задание создано")
        set_assignee(task, assignee, ADMIN_NAME)
        STATE["tasks"].append(task)
        return task

    def move(self, task, body, user):
        if user != "admin":
            raise PermissionError("Распределять задания может только замдекана")
        target = body.get("assignee") if body.get("assignee") in METHODISTS else None
        col = column(target, exclude=task)
        before = body.get("before")
        idx = next((i for i, t in enumerate(col) if t["id"] == before), len(col))
        col.insert(idx, task)
        set_assignee(task, target, ADMIN_NAME)
        for i, t in enumerate(col):
            t["order"] = i
        task["updated_at"] = now()
        task["updated_by"] = ADMIN_NAME


def nfc(s):
    return unicodedata.normalize("NFC", s)


def avatars():
    """Фото методистов: static/avatars/<Имя>.jpg (png, webp...)."""
    found = {}
    if os.path.isdir(AVATAR_DIR):
        for fn in os.listdir(AVATAR_DIR):
            stem, ext = os.path.splitext(fn)
            if ext.lower() in IMAGE_EXT and nfc(stem) in METHODISTS + [ADMIN_NAME]:
                found[nfc(stem)] = "avatars/" + fn
    return found


def logo():
    for ext in IMAGE_EXT:
        if os.path.exists(os.path.join(STATIC_DIR, "logo" + ext)):
            return "logo" + ext
    return None


def lan_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return None


if __name__ == "__main__":
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    ip = lan_ip()
    if ip:
        PUBLIC_URL = f"http://{ip}:{PORT}"
    for target in (reminder_loop, tg_sender, tg_poller):
        threading.Thread(target=target, daemon=True).start()
    print("=" * 56)
    print("  Задания методистам — сервер запущен")
    print(f"  На этом компьютере:  http://localhost:{PORT}")
    if ip:
        print(f"  Для методистов (локальная сеть):  http://{ip}:{PORT}")
    print(f"  PIN замдекана: {PIN}")
    if PIN == "1234":
        print("  Смените PIN: в start.bat (Windows) или ZAM_PIN=xxxx python3 server.py (Mac)")
    tg = tg_conf()
    print(f"  Telegram-бот: @{tg['bot']}" if tg.get("token") else "  Telegram-бот: не настроен (Настройки на сайте)")
    print("  Остановить: Ctrl+C")
    print("=" * 56)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nСервер остановлен.")
