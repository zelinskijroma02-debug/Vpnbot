"""
VPN-бот на панели 3x-ui.

Возможности:
  • выбор страны/локации (сейчас доступна Рига, остальные помечены «скоро»)
  • баланс, пополнение (ручное подтверждение админом), оплата с баланса
  • тарифы по устройствам и срокам (1 / 3 / 6 мес. со скидкой)
  • промокоды (бонус на баланс или скидка в %), создаются админом
  • реферальная программа (% с пополнений приглашённых)
  • профиль, мои ключи, пробный период, инструкция, поддержка
  • напоминание об окончании подписки
  • админ-команды: /admin, /stats, /user, /addbal, /give, /promo_*, /broadcast, /backup
  • автобэкап базы админу в Telegram + /restore

Переменные окружения:
  BOT_TOKEN        токен бота
  ADMIN_IDS        Telegram ID админов через запятую
  PANEL_URL        http://IP:порт/путь/   (панель Риги)
  PANEL_TOKEN      API-токен панели (Настройки панели → Безопасность → API-токен). Рекомендуется
  PANEL_USER, PANEL_PASS  (нужны, только если токен не задан)
  INBOUND_ID       ID inbound (VLESS Reality) в панели (нужна 3x-ui v3+, API /panel/api/clients)
  SERVER_HOST      IP или домен сервера Риги
  PAY_INFO         реквизиты для оплаты
  SUPPORT_USERNAME (необяз.) ник поддержки без @
  TRIAL_DAYS       (необяз.) дней пробного периода, 0 = выключено (по умолчанию 1)
  REF_PERCENT      (необяз.) % рефереру с пополнений, по умолчанию 20
  REF_INVITEE_BONUS (необяз.) ₽ приглашённому при первом пополнении, по умолчанию 0
  MIN_TOPUP        (необяз.) минимальное пополнение, по умолчанию 50
  DB_PATH          (необяз.) путь к SQLite-базе, по умолчанию bot.db
"""
import asyncio
import hashlib
import html
import json
import logging
import os
import re
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import quote

import aiohttp
from aiohttp import web
from aiogram import BaseMiddleware, Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)


# ---------------------------------------------------------------- настройки
def env_int(name: str, default: int | None = None) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        if default is None:
            raise SystemExit(f"Переменная {name} не задана")
        return default
    try:
        return int(raw)
    except ValueError:
        raise SystemExit(f"Переменная {name} должна быть числом, сейчас: {raw!r}")


TOKEN = os.environ["BOT_TOKEN"].strip()
ADMINS = {int(x) for x in os.environ.get("ADMIN_IDS", "").split(",") if x.strip()}
PANEL_URL = os.environ["PANEL_URL"].strip()
PANEL_USER = os.environ.get("PANEL_USER", "")
PANEL_PASS = os.environ.get("PANEL_PASS", "")
PANEL_TOKEN = os.environ.get("PANEL_TOKEN", "").strip()
INBOUND_ID = env_int("INBOUND_ID")
HOST = os.environ["SERVER_HOST"].strip()
PAY_INFO = os.environ.get("PAY_INFO", "Реквизиты уточни у администратора.")
SUPPORT = os.environ.get("SUPPORT_USERNAME", "").strip().lstrip("@")
TRIAL_DAYS = env_int("TRIAL_DAYS", 1)
REF_PERCENT = env_int("REF_PERCENT", 20)
REF_INVITEE_BONUS = env_int("REF_INVITEE_BONUS", 0)
MIN_TOPUP = env_int("MIN_TOPUP", 50)
DB_PATH = os.environ.get("DB_PATH", "bot.db")

BASE_PRICE = 150      # ₽ за 30 дней на BASE_DEVICES устройств
BASE_DEVICES = 2
EXTRA_PRICE = 50      # ₽ за каждое доп. устройство
MAX_DEVICES = 5
PERIODS = {1: 0, 3: 10, 6: 15}   # месяцев -> скидка за срок, %
DAY_MS = 86400 * 1000
REMIND_DAYS = 3

bot = Bot(TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
IS_ADMIN = F.from_user.id.in_(ADMINS)
BOT_USERNAME = ""
busy: set[int] = set()


def ms() -> int:
    return int(time.time() * 1000)


def fmt_date(t_ms: int) -> str:
    return datetime.fromtimestamp(t_ms / 1000).strftime("%d.%m.%Y")


def month_price(devices: int) -> int:
    return BASE_PRICE + max(0, devices - BASE_DEVICES) * EXTRA_PRICE


def period_price(devices: int, months: int) -> int:
    return round(month_price(devices) * months * (100 - PERIODS[months]) / 100)


def final_price(devices: int, months: int, promo_pct: int = 0) -> int:
    return max(1, round(period_price(devices, months) * (100 - promo_pct) / 100))


# ---------------------------------------------------------------- база данных
SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
    id INTEGER PRIMARY KEY, username TEXT, full_name TEXT,
    balance INTEGER NOT NULL DEFAULT 0, ref_by INTEGER, created INTEGER,
    trial_used INTEGER NOT NULL DEFAULT 0,
    discount_pct INTEGER NOT NULL DEFAULT 0, discount_code TEXT,
    ref_earned INTEGER NOT NULL DEFAULT 0,
    first_topup_done INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS topups(
    id INTEGER PRIMARY KEY AUTOINCREMENT, uid INTEGER, amount INTEGER,
    status TEXT DEFAULT 'pending', created INTEGER
);
CREATE TABLE IF NOT EXISTS promos(
    code TEXT PRIMARY KEY, type TEXT, value INTEGER,
    max_uses INTEGER DEFAULT 0, used INTEGER DEFAULT 0, expires INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS promo_uses(code TEXT, uid INTEGER, PRIMARY KEY(code, uid));
CREATE TABLE IF NOT EXISTS purchases(
    id INTEGER PRIMARY KEY AUTOINCREMENT, uid INTEGER, loc TEXT,
    devices INTEGER, months INTEGER, price INTEGER, created INTEGER
);
CREATE TABLE IF NOT EXISTS reminders(email TEXT, expiry INTEGER, PRIMARY KEY(email, expiry));
"""


class DB:
    def __init__(self, path: str):
        self.path = path
        self.lock = threading.Lock()
        self._open()

    def _open(self):
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def _run(self, sql, args, mode):
        with self.lock:
            cur = self.conn.execute(sql, args)
            if mode == "all":
                res = [dict(r) for r in cur.fetchall()]
            elif mode == "one":
                r = cur.fetchone()
                res = dict(r) if r else None
            elif mode == "rc":
                res = cur.rowcount
            else:
                res = cur.lastrowid
            self.conn.commit()
            return res

    async def all(self, sql, *a):
        return await asyncio.to_thread(self._run, sql, a, "all")

    async def one(self, sql, *a):
        return await asyncio.to_thread(self._run, sql, a, "one")

    async def rc(self, sql, *a):
        return await asyncio.to_thread(self._run, sql, a, "rc")

    async def ins(self, sql, *a):
        return await asyncio.to_thread(self._run, sql, a, "ins")

    def dump(self) -> bytes:
        tmp = self.path + ".bak"
        with self.lock:
            dst = sqlite3.connect(tmp)
            self.conn.backup(dst)
            dst.close()
        with open(tmp, "rb") as f:
            return f.read()

    def replace(self, new_path: str):
        with self.lock:
            self.conn.close()
            os.replace(new_path, self.path)
            self._open()


db = DB(DB_PATH)


# ---------------------------------------------------------------- панель 3x-ui
class PanelError(RuntimeError):
    """Панель ответила JSON с success=false (например, клиент не найден)."""


class Panel:
    """Клиент 3x-ui. Если задан token, вход не нужен (Bearer-токен, CSRF не требуется).
    Иначе: вход по логину/паролю с CSRF-токеном (новые версии 3x-ui)."""

    def __init__(self, url: str, user: str, password: str, token: str = ""):
        self.url = url.rstrip("/") + "/"
        self.user, self.password, self.token = user, password, token
        self.csrf = ""
        self.s: aiohttp.ClientSession | None = None
        self.lock = asyncio.Lock()

    async def _login(self):
        if self.s:
            await self.s.close()
        self.s = aiohttp.ClientSession(
            cookie_jar=aiohttp.CookieJar(unsafe=True),
            timeout=aiohttp.ClientTimeout(total=30),
        )
        self.csrf = ""
        if self.token:
            return
        # новые версии 3x-ui: сначала CSRF-токен, потом вход
        async with self.s.get(self.url + "csrf-token") as r:
            t = await r.text()
        try:
            j = json.loads(t)
            obj = j.get("obj") if isinstance(j, dict) else j
            if isinstance(obj, dict):
                obj = next((v for v in obj.values() if isinstance(v, str)), "")
            self.csrf = obj if isinstance(obj, str) else ""
        except ValueError:
            self.csrf = t.strip()
        headers = {"X-CSRF-Token": self.csrf} if self.csrf else {}
        async with self.s.post(
            self.url + "login",
            data={"username": self.user, "password": self.password},
            headers=headers,
        ) as r:
            text = await r.text()
            status, final_url = r.status, str(r.url)
        try:
            j = json.loads(text)
        except ValueError:
            raise RuntimeError(
                f"Панель не вернула JSON при входе (HTTP {status}, адрес {final_url}). "
                f"Задай PANEL_TOKEN (API-токен из настроек панели). Ответ: {text[:150]!r}"
            )
        if not isinstance(j, dict) or not j.get("success"):
            raise RuntimeError("Панель: неверный логин или пароль")

    async def call(self, method: str, path: str, **kw):
        async with self.lock:
            for attempt in (0, 1):
                if self.s is None or (attempt == 1 and not self.token):
                    await self._login()
                headers = {}
                if self.token:
                    headers["Authorization"] = f"Bearer {self.token}"
                elif self.csrf:
                    headers["X-CSRF-Token"] = self.csrf
                async with self.s.request(method, self.url + path, headers=headers, **kw) as r:
                    text = await r.text()
                    status = r.status
                try:
                    j = json.loads(text)
                except ValueError:
                    if self.token and status in (401, 403, 404):
                        raise RuntimeError(f"Панель отклонила запрос (HTTP {status}) {method} {path}. Проверь PANEL_TOKEN, PANEL_URL и версию панели")
                    continue
                if not isinstance(j, dict):
                    raise RuntimeError(f"Панель вернула неожиданный ответ: {text[:150]!r}")
                if not j.get("success"):
                    raise PanelError(f"Панель: {j.get('msg')}")
                return j.get("obj")
            raise RuntimeError("Панель не отвечает (проверь PANEL_URL и что порт открыт)")


@dataclass
class Location:
    code: str
    title: str
    available: bool = False
    panel: Panel | None = None
    inbound_id: int = 0
    host: str = ""

    def email(self, uid: int) -> str:
        # у Риги формат прежний, чтобы старые клиенты не потерялись
        return f"tg{uid}" if self.code == "riga" else f"tg{uid}-{self.code}"


# Чтобы включить новую страну: создай ещё одну панель/inbound и замени строку,
# например: Location("nl", "🇳🇱 Нидерланды", True, Panel(url, user, pass), inbound_id, host)
LOCATIONS: dict[str, Location] = {
    "riga": Location("riga", "🇱🇻 Рига", True, Panel(PANEL_URL, PANEL_USER, PANEL_PASS, PANEL_TOKEN), INBOUND_ID, HOST),
    "nl": Location("nl", "🇳🇱 Нидерланды"),
    "de": Location("de", "🇩🇪 Германия"),
    "fi": Location("fi", "🇫🇮 Финляндия"),
}


def avail() -> list[Location]:
    return [l for l in LOCATIONS.values() if l.available]


async def get_inbound(loc: Location) -> dict:
    inb = await loc.panel.call("GET", f"panel/api/inbounds/get/{loc.inbound_id}")
    if not inb:
        raise RuntimeError(f"Inbound с ID {loc.inbound_id} не найден в панели ({loc.title})")
    return inb


def jl(v):
    """Разбор поля панели: в новых версиях 3x-ui это уже объект, в старых - JSON-строка."""
    if isinstance(v, (dict, list)):
        return v
    return json.loads(v) if v else {}


def eq(email: str) -> str:
    return quote(email, safe="")


async def list_clients(loc: Location) -> list:
    obj = await loc.panel.call("GET", "panel/api/clients/list")
    if isinstance(obj, dict):
        obj = obj.get("items") or obj.get("clients") or []
    return obj or []


async def get_client(loc: Location, email: str):
    """Возвращает (client, inboundIds) или None, если клиента нет."""
    try:
        obj = await loc.panel.call("GET", f"panel/api/clients/get/{eq(email)}")
    except PanelError:
        return None
    if not isinstance(obj, dict):
        return None
    c = obj.get("client") if isinstance(obj.get("client"), dict) else obj
    if not c or not c.get("email"):
        return None
    return c, obj.get("inboundIds") or c.get("inboundIds") or []


async def sub_of(uid: int, loc: Location):
    r = await get_client(loc, loc.email(uid))
    return r[0] if r else None


def make_link(inb: dict, c: dict, loc: Location) -> str:
    """Запасной вариант, если панель не отдала ссылку сама."""
    st = jl(inb["streamSettings"])
    net = st.get("network", "tcp")
    sec = st.get("security", "none")
    q = {"type": net, "security": sec}
    if sec == "reality":
        rs = st["realitySettings"]
        q.update(
            pbk=rs["settings"]["publicKey"],
            fp=rs["settings"].get("fingerprint", "chrome"),
            sni=rs["serverNames"][0],
            sid=(rs.get("shortIds") or [""])[0],
            spx="/",
        )
    if c.get("flow"):
        q["flow"] = c["flow"]
    query = "&".join(f"{k}={quote(str(v), safe='')}" for k, v in q.items())
    cid = c.get("uuid") or c["id"]
    return f"vless://{cid}@{loc.host}:{inb['port']}?{query}#{quote('VPN-' + c['email'])}"


async def get_link(loc: Location, c: dict) -> str:
    try:
        obj = await loc.panel.call("GET", f"panel/api/clients/links/{eq(c['email'])}")
        items = []
        if isinstance(obj, str):
            items = [obj]
        elif isinstance(obj, list):
            items = obj
        elif isinstance(obj, dict):
            for v in obj.values():
                items += v if isinstance(v, list) else [v]
        links = [x for x in items if isinstance(x, str) and "://" in x]
        if links:
            return next((x for x in links if x.startswith("vless://")), links[0])
    except Exception:
        logging.exception("links endpoint failed, building link manually")
    return make_link(await get_inbound(loc), c, loc)


async def grant(uid: int, loc: Location, devices: int, days: int) -> dict:
    """Создаёт или продлевает клиента (API 3x-ui v3). Возвращает client."""
    email = loc.email(uid)
    now = ms()
    found = await get_client(loc, email)
    if found:
        c, ids = found
        c["expiryTime"] = max(now, c.get("expiryTime") or 0) + days * DAY_MS
        c["limitIp"] = devices
        c["enable"] = True
        c["totalGB"] = 0
        minimal = {
            "email": email,
            "enable": True,
            "expiryTime": c["expiryTime"],
            "limitIp": devices,
            "totalGB": 0,
        }
        for k in ("id", "uuid", "flow", "subId", "password", "group", "comment", "reset", "tgId"):
            if c.get(k) not in (None, ""):
                minimal[k] = c[k]
        no_email = {k: v for k, v in minimal.items() if k in ("enable", "expiryTime", "limitIp", "totalGB")}
        errors = []
        for body in (minimal, c, no_email, {"client": c, "email": email}):
            try:
                await loc.panel.call("POST", f"panel/api/clients/update/{eq(email)}", json=body)
                errors = []
                break
            except PanelError as e:
                errors.append(str(e)[:80])
        if errors:
            raise PanelError("не удалось обновить клиента: " + " | ".join(errors))
        if loc.inbound_id not in ids:
            await loc.panel.call("POST", f"panel/api/clients/{eq(email)}/attach", json={"inboundIds": [loc.inbound_id]})
        return c
    inb = await get_inbound(loc)
    st = jl(inb["streamSettings"])
    vision = st.get("network", "tcp") == "tcp" and st.get("security") in ("reality", "tls")
    c = {
        "email": email,
        "id": str(uuid.uuid4()),
        "flow": "xtls-rprx-vision" if vision else "",
        "limitIp": devices,
        "totalGB": 0,
        "expiryTime": now + days * DAY_MS,
        "enable": True,
        "subId": uuid.uuid4().hex[:16],
    }
    await loc.panel.call("POST", "panel/api/clients/add", json={"inboundIds": [loc.inbound_id], "client": c})
    return c


# ---------------------------------------------------------------- UI-хелперы
def btn(text: str, data: str | None = None, url: str | None = None) -> InlineKeyboardButton:
    if url:
        return InlineKeyboardButton(text=text, url=url)
    return InlineKeyboardButton(text=text, callback_data=data)


def kb(rows) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=rows)


BACK = [btn("⬅️ В меню", "menu")]


async def show(cb: CallbackQuery, text: str, markup=None):
    try:
        await cb.message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest as e:
        if "not modified" not in str(e):
            await cb.message.answer(text, reply_markup=markup)


async def get_user(uid: int) -> dict:
    return await db.one("SELECT * FROM users WHERE id=?", uid)


async def main_kb(uid: int) -> InlineKeyboardMarkup:
    u = await get_user(uid)
    rows = [
        [btn("💳 Купить / продлить", "buy")],
        [btn("🔑 Мои ключи", "keys"), btn("👤 Профиль", "profile")],
        [btn("💰 Пополнить", "topup"), btn("🎟 Промокод", "promo")],
        [btn("👥 Рефералы", "refs"), btn("📖 Инструкция", "help")],
    ]
    if TRIAL_DAYS > 0 and u and not u["trial_used"]:
        rows.insert(0, [btn(f"🎁 Попробовать бесплатно ({TRIAL_DAYS} дн.)", "trial")])
    if SUPPORT:
        rows.append([btn("🆘 Поддержка", url=f"https://t.me/{SUPPORT}")])
    return kb(rows)


async def menu_text(uid: int, name: str) -> str:
    u = await get_user(uid)
    return (
        f"👋 Привет, {html.escape(name)}!\n\n"
        f"Быстрый VPN без ограничения трафика.\n"
        f"От <b>{BASE_PRICE} ₽</b> за 30 дней, {BASE_DEVICES} устройства в базе, "
        f"каждое следующее +{EXTRA_PRICE} ₽.\n\n"
        f"💰 Баланс: <b>{u['balance']} ₽</b>"
    )


async def notify_admins(text: str, markup=None):
    for a in ADMINS:
        try:
            await bot.send_message(a, text, reply_markup=markup)
        except Exception:
            logging.exception("не смог написать админу %s", a)


async def send_key(uid: int, c: dict, loc: Location):
    link = await get_link(loc, c)
    await bot.send_message(
        uid,
        f"🔑 <b>{loc.title}</b>\n"
        f"Действует до <b>{fmt_date(c['expiryTime'])}</b>, устройств: {c['limitIp']}.\n\n"
        f"Твой ключ (нажми, чтобы скопировать):\n<code>{html.escape(link)}</code>\n\n"
        "Приложения: v2rayNG (Android), Streisand или Happ (iPhone), "
        "v2rayN / Hiddify (ПК). Добавь ключ из буфера обмена.",
    )


# ---------------------------------------------------------------- регистрация пользователей
class EnsureUser(BaseMiddleware):
    async def __call__(self, handler, event, data):
        u = getattr(event, "from_user", None)
        if u and not u.is_bot:
            ref = None
            if isinstance(event, Message) and event.text and event.text.startswith("/start"):
                parts = event.text.split()
                if len(parts) > 1 and parts[1].startswith("ref_") and parts[1][4:].isdigit():
                    ref = int(parts[1][4:])
            await ensure_user(u, ref)
        return await handler(event, data)


async def ensure_user(u, ref: int | None):
    name = u.full_name or ""
    if await get_user(u.id) is None:
        ref_by = ref if ref and ref != u.id and await get_user(ref) else None
        await db.rc(
            "INSERT OR IGNORE INTO users(id,username,full_name,ref_by,created) VALUES(?,?,?,?,?)",
            u.id, u.username, name, ref_by, int(time.time()),
        )
        if ref_by:
            try:
                await bot.send_message(ref_by, "👥 По твоей ссылке зарегистрировался новый пользователь!")
            except Exception:
                pass
    else:
        await db.rc("UPDATE users SET username=?, full_name=? WHERE id=?", u.username, name, u.id)


dp.message.outer_middleware(EnsureUser())
dp.callback_query.outer_middleware(EnsureUser())


class St(StatesGroup):
    promo = State()
    amount = State()


# ---------------------------------------------------------------- меню
@dp.message(Command("start"))
async def start(m: Message, state: FSMContext):
    await state.clear()
    await m.answer(await menu_text(m.from_user.id, m.from_user.first_name), reply_markup=await main_kb(m.from_user.id))


@dp.callback_query(F.data == "menu")
async def menu(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await show(cb, await menu_text(cb.from_user.id, cb.from_user.first_name), await main_kb(cb.from_user.id))
    await cb.answer()


@dp.callback_query(F.data == "cancel")
async def cancel(cb: CallbackQuery, state: FSMContext):
    await menu(cb, state)


@dp.callback_query(F.data == "soon")
async def soon(cb: CallbackQuery):
    await cb.answer("Эта локация скоро появится 🚀", show_alert=True)


@dp.callback_query(F.data == "help")
async def help_(cb: CallbackQuery):
    await show(
        cb,
        "📖 <b>Как подключиться</b>\n\n"
        "1. Купи подписку и получи ключ в разделе «🔑 Мои ключи».\n"
        "2. Скопируй ключ (нажми на него).\n"
        "3. Установи приложение:\n"
        "• iPhone: Streisand или Happ\n"
        "• Android: v2rayNG или Happ\n"
        "• ПК: v2rayN или Hiddify\n"
        "4. В приложении выбери «Добавить из буфера обмена» и включи VPN.\n\n"
        "Один ключ работает на количество устройств из твоего тарифа.",
        kb([BACK]),
    )
    await cb.answer()


# ---------------------------------------------------------------- профиль
@dp.callback_query(F.data == "profile")
async def profile(cb: CallbackQuery):
    await cb.answer()
    uid = cb.from_user.id
    u = await get_user(uid)
    refs = (await db.one("SELECT COUNT(*) n FROM users WHERE ref_by=?", uid))["n"]
    lines = []
    for loc in avail():
        try:
            c = await sub_of(uid, loc)
        except Exception:
            lines.append(f"{loc.title}: не удалось проверить")
            continue
        if not c:
            continue
        exp = c.get("expiryTime") or 0
        if exp < ms():
            lines.append(f"{loc.title}: ❌ закончилась {fmt_date(exp)}")
        else:
            lines.append(f"{loc.title}: ✅ до {fmt_date(exp)}, устр.: {c.get('limitIp')}")
    text = (
        f"👤 <b>Профиль</b>\n\n"
        f"ID: <code>{uid}</code>\n"
        f"💰 Баланс: <b>{u['balance']} ₽</b>\n"
    )
    if u["discount_pct"]:
        text += f"🎟 Скидка на следующую покупку: <b>{u['discount_pct']}%</b>\n"
    text += "\n<b>Подписки</b>\n" + ("\n".join(lines) if lines else "пока нет")
    text += f"\n\n👥 Приглашено: {refs}, заработано: {u['ref_earned']} ₽"
    await show(cb, text, kb([[btn("💰 Пополнить", "topup"), btn("💳 Купить", "buy")], BACK]))


@dp.callback_query(F.data == "keys")
async def keys(cb: CallbackQuery):
    await cb.answer()
    uid = cb.from_user.id
    found = False
    for loc in avail():
        try:
            c = await sub_of(uid, loc)
        except Exception as e:
            logging.exception("panel error")
            await cb.message.answer(f"{loc.title}: сервер не отвечает, попробуй позже.")
            continue
        if not c:
            continue
        found = True
        if (c.get("expiryTime") or 0) < ms():
            await cb.message.answer(
                f"{loc.title}: подписка закончилась {fmt_date(c['expiryTime'])}. Продли её.",
                reply_markup=kb([[btn("💳 Продлить", "buy")]]),
            )
        else:
            await send_key(uid, c, loc)
    if not found:
        await cb.message.answer("Подписки пока нет.", reply_markup=await main_kb(uid))


# ---------------------------------------------------------------- пробный период
@dp.callback_query(F.data == "trial")
async def trial(cb: CallbackQuery):
    uid = cb.from_user.id
    loc = avail()[0]
    if TRIAL_DAYS <= 0 or uid in busy:
        return await cb.answer("Недоступно", show_alert=True)
    if await db.rc("UPDATE users SET trial_used=1 WHERE id=? AND trial_used=0", uid) == 0:
        return await cb.answer("Пробный период уже использован", show_alert=True)
    busy.add(uid)
    await cb.answer("Создаю ключ...")
    try:
        c = await sub_of(uid, loc)
        if c:
            raise ValueError("exists")
        c = await grant(uid, loc, 1, TRIAL_DAYS)
        await send_key(uid, c, loc)
        await cb.message.answer("🎁 Пробный период активирован. Понравится — продли в «Купить».",
                                reply_markup=await main_kb(uid))
    except ValueError:
        await cb.message.answer("Пробный период доступен только новым пользователям.")
    except Exception as e:
        logging.exception("trial failed")
        await db.rc("UPDATE users SET trial_used=0 WHERE id=?", uid)
        await cb.message.answer("Не получилось выдать ключ. Попробуй позже или напиши в поддержку.")
        await notify_admins(f"⚠️ Ошибка пробного периода у {uid}: {html.escape(str(e))}")
    finally:
        busy.discard(uid)


# ---------------------------------------------------------------- покупка
@dp.callback_query(F.data == "buy")
async def buy(cb: CallbackQuery):
    rows = []
    for loc in LOCATIONS.values():
        if loc.available:
            rows.append([btn(loc.title, f"loc:{loc.code}")])
        else:
            rows.append([btn(f"{loc.title} — скоро", "soon")])
    rows.append(BACK)
    await show(cb, "🌍 <b>Выбери локацию</b>", kb(rows))
    await cb.answer()


@dp.callback_query(F.data.startswith("loc:"))
async def pick_loc(cb: CallbackQuery):
    loc = LOCATIONS.get(cb.data.split(":")[1])
    if not loc or not loc.available:
        return await cb.answer("Локация недоступна", show_alert=True)
    rows = [
        [btn(f"{n} устр. — от {month_price(n)} ₽ / мес.", f"dev:{loc.code}:{n}")]
        for n in range(BASE_DEVICES, MAX_DEVICES + 1)
    ]
    rows.append([btn("⬅️ Назад", "buy")])
    await show(cb, f"{loc.title}\n\n📱 <b>Сколько устройств подключить?</b>", kb(rows))
    await cb.answer()


@dp.callback_query(F.data.startswith("dev:"))
async def pick_dev(cb: CallbackQuery):
    _, code, n = cb.data.split(":")
    n = int(n)
    u = await get_user(cb.from_user.id)
    rows = []
    for m_, disc in PERIODS.items():
        label = f"{m_} мес. — {final_price(n, m_, u['discount_pct'])} ₽"
        if disc:
            label += f" (−{disc}%)"
        rows.append([btn(label, f"per:{code}:{n}:{m_}")])
    rows.append([btn("⬅️ Назад", f"loc:{code}")])
    await show(cb, f"{LOCATIONS[code].title} · {n} устр.\n\n⏳ <b>На какой срок?</b>", kb(rows))
    await cb.answer()


@dp.callback_query(F.data.startswith("per:"))
async def pick_period(cb: CallbackQuery):
    _, code, n, m_ = cb.data.split(":")
    n, m_ = int(n), int(m_)
    loc = LOCATIONS[code]
    u = await get_user(cb.from_user.id)
    cost = final_price(n, m_, u["discount_pct"])
    text = (
        f"🧾 <b>Заказ</b>\n\n"
        f"📍 Локация: {loc.title}\n📱 Устройств: {n}\n⏳ Срок: {m_} мес.\n"
    )
    if PERIODS[m_]:
        text += f"🏷 Скидка за срок: {PERIODS[m_]}%\n"
    if u["discount_pct"]:
        text += f"🎟 Промокод: −{u['discount_pct']}%\n"
    text += f"\n<b>К оплате: {cost} ₽</b>\n💰 Твой баланс: {u['balance']} ₽"
    rows = []
    if u["balance"] >= cost:
        rows.append([btn(f"✅ Оплатить {cost} ₽ с баланса", f"do:{code}:{n}:{m_}")])
    else:
        need = max(cost - u["balance"], MIN_TOPUP)
        text += f"\n\nНе хватает {cost - u['balance']} ₽."
        rows.append([btn(f"➕ Пополнить на {need} ₽", f"top:{need}")])
    rows.append([btn("🎟 Ввести промокод", "promo")])
    rows.append([btn("⬅️ Назад", f"dev:{code}:{n}")])
    await show(cb, text, kb(rows))
    await cb.answer()


@dp.callback_query(F.data.startswith("do:"))
async def do_pay(cb: CallbackQuery):
    _, code, n, m_ = cb.data.split(":")
    n, m_ = int(n), int(m_)
    loc = LOCATIONS.get(code)
    uid = cb.from_user.id
    if not loc or not loc.available or m_ not in PERIODS or not BASE_DEVICES <= n <= MAX_DEVICES:
        return await cb.answer("Неверный заказ", show_alert=True)
    if uid in busy:
        return await cb.answer("Подожди, заказ обрабатывается...")
    u = await get_user(uid)
    cost = final_price(n, m_, u["discount_pct"])
    if await db.rc("UPDATE users SET balance=balance-? WHERE id=? AND balance>=?", cost, uid, cost) == 0:
        await cb.answer("Недостаточно средств", show_alert=True)
        return await pick_period(cb)
    busy.add(uid)
    await cb.answer("Оформляю...")
    try:
        c = await grant(uid, loc, n, 30 * m_)
    except Exception as e:
        logging.exception("grant failed")
        await db.rc("UPDATE users SET balance=balance+? WHERE id=?", cost, uid)
        await show(cb, "😔 Не получилось выдать ключ. Деньги вернулись на баланс, попробуй позже.", kb([BACK]))
        await notify_admins(f"⚠️ Ошибка выдачи для {uid}: {html.escape(str(e))}")
        return
    finally:
        busy.discard(uid)
    if u["discount_pct"]:
        await db.rc("UPDATE users SET discount_pct=0, discount_code=NULL WHERE id=?", uid)
    await db.ins(
        "INSERT INTO purchases(uid,loc,devices,months,price,created) VALUES(?,?,?,?,?,?)",
        uid, code, n, m_, cost, int(time.time()),
    )
    await show(cb, f"✅ Оплачено <b>{cost} ₽</b>. Подписка активна до {fmt_date(c['expiryTime'])}.", kb([BACK]))
    await send_key(uid, c, loc)


# ---------------------------------------------------------------- пополнение
@dp.callback_query(F.data == "topup")
async def topup(cb: CallbackQuery):
    rows = [
        [btn("100 ₽", "top:100"), btn("150 ₽", "top:150")],
        [btn("300 ₽", "top:300"), btn("500 ₽", "top:500")],
        [btn("✏️ Другая сумма", "top:custom")],
        BACK,
    ]
    await show(cb, "💰 <b>Пополнение баланса</b>\n\nВыбери сумму:", kb(rows))
    await cb.answer()


def pay_screen(amount: int):
    text = (
        f"💳 <b>Пополнение на {amount} ₽</b>\n\n{html.escape(PAY_INFO)}\n\n"
        f"После перевода нажми кнопку, админ проверит и зачислит деньги."
    )
    return text, kb([[btn("✅ Я оплатил", f"paid:{amount}")], [btn("✖ Отмена", "cancel")]])


@dp.callback_query(F.data.startswith("top:"))
async def top_amount(cb: CallbackQuery, state: FSMContext):
    val = cb.data.split(":")[1]
    if val == "custom":
        await state.set_state(St.amount)
        await show(cb, f"✏️ Отправь сумму числом (от {MIN_TOPUP} до 50000 ₽).", kb([[btn("✖ Отмена", "cancel")]]))
        return await cb.answer()
    text, markup = pay_screen(int(val))
    await show(cb, text, markup)
    await cb.answer()


@dp.message(St.amount, F.text, ~F.text.startswith("/"))
async def top_custom(m: Message, state: FSMContext):
    raw = m.text.strip().replace(" ", "")
    if not raw.isdigit() or not MIN_TOPUP <= int(raw) <= 50000:
        return await m.answer(f"Нужно число от {MIN_TOPUP} до 50000. Попробуй ещё раз.")
    await state.clear()
    text, markup = pay_screen(int(raw))
    await m.answer(text, reply_markup=markup)


@dp.callback_query(F.data.startswith("paid:"))
async def paid(cb: CallbackQuery):
    amount = int(cb.data.split(":")[1])
    u = cb.from_user
    pend = (await db.one("SELECT COUNT(*) n FROM topups WHERE uid=? AND status='pending'", u.id))["n"]
    if pend >= 3:
        return await cb.answer("У тебя уже есть заявки на проверке, подожди.", show_alert=True)
    tid = await db.ins("INSERT INTO topups(uid,amount,created) VALUES(?,?,?)", u.id, amount, int(time.time()))
    name = f"@{u.username}" if u.username else u.full_name
    await notify_admins(
        f"💰 Заявка #{tid}\nОт {html.escape(name)} (id <code>{u.id}</code>)\nСумма: <b>{amount} ₽</b>",
        kb([[btn("✅ Зачислить", f"tok:{tid}"), btn("❌ Отклонить", f"tno:{tid}")]]),
    )
    await show(cb, "⏳ Заявка отправлена. Как только оплату проверят, баланс пополнится, я напишу.", kb([BACK]))
    await cb.answer()


# ---------------------------------------------------------------- промокоды
async def redeem(uid: int, raw: str) -> str:
    code = raw.strip().upper()
    p = await db.one("SELECT * FROM promos WHERE code=?", code)
    if not p:
        return "❌ Такого промокода нет."
    if p["expires"] and p["expires"] < time.time():
        return "⌛ Срок действия промокода истёк."
    if p["max_uses"] and p["used"] >= p["max_uses"]:
        return "😕 Промокод уже закончился."
    u = await get_user(uid)
    if p["type"] == "percent" and u["discount_pct"] >= p["value"]:
        return f"У тебя уже есть скидка {u['discount_pct']}%, она не меньше."
    if await db.rc("INSERT OR IGNORE INTO promo_uses(code,uid) VALUES(?,?)", code, uid) == 0:
        return "Ты уже использовал этот промокод."
    if await db.rc("UPDATE promos SET used=used+1 WHERE code=? AND (max_uses=0 OR used<max_uses)", code) == 0:
        await db.rc("DELETE FROM promo_uses WHERE code=? AND uid=?", code, uid)
        return "😕 Промокод уже закончился."
    if p["type"] == "bonus":
        await db.rc("UPDATE users SET balance=balance+? WHERE id=?", p["value"], uid)
        return f"✅ Промокод активирован! На баланс начислено <b>{p['value']} ₽</b>."
    await db.rc("UPDATE users SET discount_pct=?, discount_code=? WHERE id=?", p["value"], code, uid)
    return f"✅ Промокод активирован! Скидка <b>{p['value']}%</b> на следующую покупку."


@dp.callback_query(F.data == "promo")
async def promo_ask(cb: CallbackQuery, state: FSMContext):
    await state.set_state(St.promo)
    await show(cb, "🎟 Отправь промокод одним сообщением.", kb([[btn("✖ Отмена", "cancel")]]))
    await cb.answer()


@dp.message(St.promo, F.text, ~F.text.startswith("/"))
async def promo_in(m: Message, state: FSMContext):
    await state.clear()
    msg = await redeem(m.from_user.id, m.text)
    await m.answer(msg, reply_markup=await main_kb(m.from_user.id))


# ---------------------------------------------------------------- рефералка
@dp.callback_query(F.data == "refs")
async def refs(cb: CallbackQuery):
    uid = cb.from_user.id
    u = await get_user(uid)
    n = (await db.one("SELECT COUNT(*) n FROM users WHERE ref_by=?", uid))["n"]
    link = f"https://t.me/{BOT_USERNAME}?start=ref_{uid}"
    text = (
        "👥 <b>Реферальная программа</b>\n\n"
        f"Приглашай друзей и получай <b>{REF_PERCENT}%</b> с каждого их пополнения на свой баланс."
    )
    if REF_INVITEE_BONUS:
        text += f"\nДруг получит <b>{REF_INVITEE_BONUS} ₽</b> бонусом при первом пополнении."
    text += (
        f"\n\nТвоя ссылка:\n<code>{link}</code>\n\n"
        f"Приглашено: <b>{n}</b>\nЗаработано: <b>{u['ref_earned']} ₽</b>"
    )
    await show(cb, text, kb([BACK]))
    await cb.answer()


# ---------------------------------------------------------------- админ: заявки на пополнение
@dp.callback_query(F.data.startswith("tok:"))
async def topup_ok(cb: CallbackQuery):
    if cb.from_user.id not in ADMINS:
        return await cb.answer("Нет доступа", show_alert=True)
    tid = int(cb.data.split(":")[1])
    t = await db.one("SELECT * FROM topups WHERE id=?", tid)
    if not t or await db.rc("UPDATE topups SET status='ok' WHERE id=? AND status='pending'", tid) == 0:
        return await cb.answer("Заявка уже обработана", show_alert=True)
    uid, amount = t["uid"], t["amount"]
    await db.rc("UPDATE users SET balance=balance+? WHERE id=?", amount, uid)
    u = await get_user(uid)
    extra = ""
    if u["ref_by"]:
        bonus = amount * REF_PERCENT // 100
        if bonus > 0:
            await db.rc("UPDATE users SET balance=balance+?, ref_earned=ref_earned+? WHERE id=?", bonus, bonus, u["ref_by"])
            try:
                await bot.send_message(u["ref_by"], f"👥 Твой реферал пополнил баланс. Тебе начислено <b>{bonus} ₽</b>!")
            except Exception:
                pass
        if REF_INVITEE_BONUS and not u["first_topup_done"]:
            await db.rc("UPDATE users SET balance=balance+? WHERE id=?", REF_INVITEE_BONUS, uid)
            extra = f"\n🎁 Бонус за первое пополнение: +{REF_INVITEE_BONUS} ₽"
    await db.rc("UPDATE users SET first_topup_done=1 WHERE id=?", uid)
    u = await get_user(uid)
    try:
        await bot.send_message(
            uid, f"✅ Баланс пополнен на <b>{amount} ₽</b>.{extra}\nТеперь на балансе: <b>{u['balance']} ₽</b>",
            reply_markup=kb([[btn("💳 Купить / продлить", "buy")]]),
        )
    except Exception:
        pass
    await cb.message.edit_text(f"{cb.message.html_text}\n\n✅ Зачислено")
    await cb.answer()


@dp.callback_query(F.data.startswith("tno:"))
async def topup_no(cb: CallbackQuery):
    if cb.from_user.id not in ADMINS:
        return await cb.answer("Нет доступа", show_alert=True)
    tid = int(cb.data.split(":")[1])
    t = await db.one("SELECT * FROM topups WHERE id=?", tid)
    if not t or await db.rc("UPDATE topups SET status='no' WHERE id=? AND status='pending'", tid) == 0:
        return await cb.answer("Заявка уже обработана", show_alert=True)
    try:
        await bot.send_message(t["uid"], "Оплата не подтверждена. Если это ошибка, напиши администратору.")
    except Exception:
        pass
    await cb.message.edit_text(f"{cb.message.html_text}\n\n❌ Отклонено")
    await cb.answer()


# ---------------------------------------------------------------- админ: команды
@dp.message(Command("admin"), IS_ADMIN)
async def admin_help(m: Message):
    await m.answer(
        "🛠 <b>Админ-команды</b>\n\n"
        "/stats — статистика\n"
        "/user <code>ID</code> — карточка пользователя\n"
        "/addbal <code>ID сумма</code> — изменить баланс (можно минус)\n"
        "/give <code>ID дней [устройств]</code> — выдать подписку вручную\n"
        "/promo_add <code>КОД bonus|percent значение [лимит] [дней]</code>\n"
        "   например: /promo_add START50 bonus 50 100 30\n"
        "   или: /promo_add SALE20 percent 20\n"
        "/promo_list — список промокодов\n"
        "/promo_del <code>КОД</code> — удалить\n"
        "/broadcast <code>текст</code> — рассылка всем\n"
        "/backup — скачать базу\n"
        "Восстановление: отправь файл базы с подписью /restore"
    )


@dp.message(Command("stats"), IS_ADMIN)
async def stats(m: Message):
    now = int(time.time())
    users = (await db.one("SELECT COUNT(*) n FROM users"))["n"]
    new24 = (await db.one("SELECT COUNT(*) n FROM users WHERE created>?", now - 86400))["n"]
    income = (await db.one("SELECT COALESCE(SUM(amount),0) s FROM topups WHERE status='ok'"))["s"]
    income30 = (await db.one("SELECT COALESCE(SUM(amount),0) s FROM topups WHERE status='ok' AND created>?", now - 30 * 86400))["s"]
    pend = (await db.one("SELECT COUNT(*) n FROM topups WHERE status='pending'"))["n"]
    lines = []
    for loc in avail():
        try:
            cl = await list_clients(loc)
            act = [c for c in cl if (c.get("expiryTime") or 0) > ms()]
            lines.append(f"{loc.title}: клиентов {len(cl)}, активных {len(act)}")
        except Exception as e:
            lines.append(f"{loc.title}: ошибка панели ({html.escape(str(e))})")
    await m.answer(
        f"📊 <b>Статистика</b>\n\nПользователей: {users} (+{new24} за сутки)\n"
        f"Пополнений всего: {income} ₽ (за 30 дн.: {income30} ₽)\n"
        f"Заявок в ожидании: {pend}\n\n" + "\n".join(lines)
    )


@dp.message(Command("user"), IS_ADMIN)
async def user_info(m: Message, command: CommandObject):
    if not (command.args or "").strip().isdigit():
        return await m.answer("Использование: /user ID")
    uid = int(command.args.strip())
    u = await get_user(uid)
    if not u:
        return await m.answer("Не найден.")
    refs_n = (await db.one("SELECT COUNT(*) n FROM users WHERE ref_by=?", uid))["n"]
    spent = (await db.one("SELECT COALESCE(SUM(price),0) s FROM purchases WHERE uid=?", uid))["s"]
    await m.answer(
        f"👤 <code>{uid}</code> @{u['username'] or '—'} {html.escape(u['full_name'] or '')}\n"
        f"Баланс: {u['balance']} ₽, потрачено: {spent} ₽\n"
        f"Пригласил: {refs_n}, пришёл от: {u['ref_by'] or '—'}\n"
        f"Скидка: {u['discount_pct']}%, триал: {'использован' if u['trial_used'] else 'нет'}"
    )


@dp.message(Command("addbal"), IS_ADMIN)
async def addbal(m: Message, command: CommandObject):
    a = (command.args or "").split()
    try:
        uid, amount = int(a[0]), int(a[1])
    except (IndexError, ValueError):
        return await m.answer("Использование: /addbal ID сумма")
    if not await get_user(uid):
        return await m.answer("Пользователь не найден.")
    await db.rc("UPDATE users SET balance=balance+? WHERE id=?", amount, uid)
    u = await get_user(uid)
    await m.answer(f"Готово. Баланс {uid}: {u['balance']} ₽")
    if amount > 0:
        try:
            await bot.send_message(uid, f"💰 Тебе начислено <b>{amount} ₽</b>. Баланс: {u['balance']} ₽")
        except Exception:
            pass


@dp.message(Command("give"), IS_ADMIN)
async def give(m: Message, command: CommandObject):
    a = (command.args or "").split()
    try:
        uid, days = int(a[0]), int(a[1])
        devices = int(a[2]) if len(a) > 2 else BASE_DEVICES
    except (IndexError, ValueError):
        return await m.answer("Использование: /give ID дней [устройств]")
    loc = avail()[0]
    try:
        c = await grant(uid, loc, devices, days)
        await send_key(uid, c, loc)
        await m.answer(f"✅ Выдано {uid} на {days} дн. (до {fmt_date(c['expiryTime'])})")
    except Exception as e:
        await m.answer(f"Ошибка: {html.escape(str(e))}")


@dp.message(Command("promo_add"), IS_ADMIN)
async def promo_add(m: Message, command: CommandObject):
    a = (command.args or "").split()
    if len(a) < 3 or a[1] not in ("bonus", "percent") or not a[2].isdigit():
        return await m.answer("Использование: /promo_add КОД bonus|percent значение [лимит] [дней]")
    code, typ, val = a[0].upper(), a[1], int(a[2])
    if typ == "percent" and not 1 <= val <= 100:
        return await m.answer("Процент должен быть от 1 до 100.")
    limit = int(a[3]) if len(a) > 3 and a[3].isdigit() else 0
    days = int(a[4]) if len(a) > 4 and a[4].isdigit() else 0
    exp = int(time.time()) + days * 86400 if days else 0
    if await db.rc("INSERT OR IGNORE INTO promos(code,type,value,max_uses,expires) VALUES(?,?,?,?,?)",
                   code, typ, val, limit, exp) == 0:
        return await m.answer("Такой промокод уже есть. Удали его через /promo_del.")
    what = f"{val} ₽ на баланс" if typ == "bonus" else f"скидка {val}%"
    await m.answer(f"✅ Промокод <code>{html.escape(code)}</code>: {what}, "
                   f"лимит: {limit or '∞'}, срок: {str(days) + ' дн.' if days else '∞'}")


@dp.message(Command("promo_list"), IS_ADMIN)
async def promo_list(m: Message):
    rows = await db.all("SELECT * FROM promos ORDER BY rowid DESC LIMIT 50")
    if not rows:
        return await m.answer("Промокодов нет.")
    out = []
    for p in rows:
        what = f"{p['value']} ₽" if p["type"] == "bonus" else f"{p['value']}%"
        exp = datetime.fromtimestamp(p["expires"]).strftime("%d.%m.%Y") if p["expires"] else "∞"
        out.append(f"<code>{html.escape(p['code'])}</code> · {what} · {p['used']}/{p['max_uses'] or '∞'} · до {exp}")
    await m.answer("\n".join(out))


@dp.message(Command("promo_del"), IS_ADMIN)
async def promo_del(m: Message, command: CommandObject):
    code = (command.args or "").strip().upper()
    n = await db.rc("DELETE FROM promos WHERE code=?", code)
    await m.answer("Удалён." if n else "Не найден.")


@dp.message(Command("broadcast"), IS_ADMIN)
async def broadcast(m: Message, command: CommandObject):
    text = (command.args or "").strip()
    if not text:
        return await m.answer("Использование: /broadcast текст (можно HTML-теги <b>, <i>)")
    ids = [r["id"] for r in await db.all("SELECT id FROM users")]
    ok = 0
    for uid in ids:
        try:
            await bot.send_message(uid, text)
            ok += 1
        except Exception:
            pass
        await asyncio.sleep(0.05)
    await m.answer(f"Отправлено: {ok} из {len(ids)}")


async def send_backup(to: int | None = None, caption: str = "Бэкап базы"):
    data = await asyncio.to_thread(db.dump)
    f = BufferedInputFile(data, filename=f"bot_{datetime.now():%Y%m%d_%H%M}.db")
    cap = caption + "\nДля восстановления отправь этот файл боту с подписью /restore"
    for a in ([to] if to else ADMINS):
        try:
            await bot.send_document(a, f, caption=cap)
        except Exception:
            logging.exception("backup send failed")
    return hashlib.md5(data).hexdigest()


@dp.message(Command("backup"), IS_ADMIN)
async def backup(m: Message):
    await send_backup(m.from_user.id)


@dp.message(F.document, IS_ADMIN)
async def restore(m: Message):
    if not (m.caption or "").startswith("/restore"):
        return
    tmp = DB_PATH + ".incoming"
    await bot.download(m.document, destination=tmp)
    try:
        c = sqlite3.connect(tmp)
        c.execute("SELECT COUNT(*) FROM users")
        c.close()
    except Exception:
        os.remove(tmp)
        return await m.answer("Файл не похож на базу этого бота.")
    await asyncio.to_thread(db.replace, tmp)
    await m.answer("✅ База восстановлена.")


# ---------------------------------------------------------------- фоновые задачи
async def remind_expiring():
    now = ms()
    for loc in avail():
        for c in await list_clients(loc):
            exp = c.get("expiryTime") or 0
            if not now < exp < now + REMIND_DAYS * DAY_MS:
                continue
            m = re.fullmatch(r"tg(\d+)(?:-[a-z]+)?", c.get("email") or "")
            if not m or loc.email(int(m.group(1))) != c["email"]:
                continue
            tg = int(m.group(1))
            if await db.rc("INSERT OR IGNORE INTO reminders(email,expiry) VALUES(?,?)", c["email"], exp) == 0:
                continue
            try:
                await bot.send_message(
                    tg,
                    f"⏳ Подписка ({loc.title}) заканчивается <b>{fmt_date(exp)}</b>. Продли, чтобы не остаться без VPN.",
                    reply_markup=kb([[btn("💳 Продлить", "buy")]]),
                )
            except Exception:
                pass


async def background():
    await asyncio.sleep(20)
    last_hash = None
    while True:
        try:
            await remind_expiring()
        except Exception:
            logging.exception("remind failed")
        try:
            if (await db.one("SELECT COUNT(*) n FROM users"))["n"] > 0:
                data = await asyncio.to_thread(db.dump)
                h = hashlib.md5(data).hexdigest()
                if h != last_hash:
                    last_hash = await send_backup(caption="🗄 Автобэкап базы")
        except Exception:
            logging.exception("backup failed")
        await asyncio.sleep(6 * 3600)


# ---------------------------------------------------------------- запуск
async def health(_request):
    return web.Response(text="ok")


async def start_web():
    app = web.Application()
    app.router.add_get("/", health)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", "10000"))
    await web.TCPSite(runner, "0.0.0.0", port).start()


async def main():
    global BOT_USERNAME
    logging.basicConfig(level=logging.INFO)
    BOT_USERNAME = (await bot.get_me()).username
    if os.environ.get("PORT"):  # веб-порт нужен только на Render
        await start_web()
    asyncio.create_task(background())
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
