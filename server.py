#!/usr/bin/env python3
"""Локальный сервер «Задания методистам».

Запуск:  python3 server.py
Без внешних зависимостей (только стандартная библиотека Python 3).
Настройки через переменные окружения:
  PORT     — порт (по умолчанию 8080)
  ZAM_PIN  — PIN-код замдекана (по умолчанию 1234)
"""
import json
import mimetypes
import os
import re
import socket
import threading
import time
import unicodedata
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(ROOT, "data")
DATA_FILE = os.path.join(DATA_DIR, "tasks.json")
STATIC_DIR = os.path.join(ROOT, "static")
INDEX_FILE = os.path.join(STATIC_DIR, "index.html")
AVATAR_DIR = os.path.join(STATIC_DIR, "avatars")
IMAGE_EXT = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".svg")
ONLINE_SECONDS = 15

PORT = int(os.environ.get("PORT", "8080"))
PIN = os.environ.get("ZAM_PIN", "1234")

METHODISTS = ["Эльмира", "Улзира", "Перизат"]
STATUSES = ["new", "progress", "review", "done"]
STATUS_RU = {"new": "Новое", "progress": "В работе", "review": "На проверке", "done": "Выполнено"}
PRIORITIES = ["low", "normal", "high"]
ADMIN_NAME = "Замдекана"

lock = threading.Lock()
PRESENCE = {}  # кто открыл доску: имя -> время последнего опроса


def now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def load():
    if not os.path.exists(DATA_FILE):
        return {"version": 1, "tasks": []}
    with open(DATA_FILE, encoding="utf-8") as f:
        return json.load(f)


def save(state):
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = DATA_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)
    os.replace(tmp, DATA_FILE)


STATE = load()


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

    def snapshot(self):
        t = time.time()
        online = [n for n, seen in PRESENCE.items() if t - seen < ONLINE_SECONDS]
        return {"version": STATE["version"], "tasks": STATE["tasks"], "methodists": METHODISTS,
                "online": online, "avatars": avatars(), "logo": logo()}

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
            with lock:
                if who_ in METHODISTS or who_ == "admin":
                    PRESENCE[who_] = time.time()
                return self.send_json(200, self.snapshot())
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

        parts = path.strip("/").split("/")
        with lock:
            try:
                if parts == ["api", "tasks"]:
                    task = self.create(body, user)
                elif len(parts) in (3, 4) and parts[:2] == ["api", "tasks"]:
                    task = next((t for t in STATE["tasks"] if t["id"] == parts[2]), None)
                    if task is None:
                        return self.send_json(404, {"error": "Задание не найдено (возможно, удалено)"})
                    action = parts[3] if len(parts) == 4 else "update"
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
                else:
                    return self.send_json(404, {"error": "Не найдено"})
            except PermissionError as e:
                return self.send_json(403, {"error": str(e)})
            except ValueError as e:
                return self.send_json(400, {"error": str(e)})

            STATE["version"] += 1
            save(STATE)
            self.send_json(200, {"ok": True, "task": task, **self.snapshot()})

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
    print("=" * 56)
    print("  Задания методистам — сервер запущен")
    print(f"  На этом компьютере:  http://localhost:{PORT}")
    if ip:
        print(f"  Для методистов (локальная сеть):  http://{ip}:{PORT}")
    print(f"  PIN замдекана: {PIN}")
    if PIN == "1234":
        print("  Смените PIN: в start.bat (Windows) или ZAM_PIN=xxxx python3 server.py (Mac)")
    print("  Остановить: Ctrl+C")
    print("=" * 56)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nСервер остановлен.")
