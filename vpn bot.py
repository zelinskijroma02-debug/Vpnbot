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
  CRYPTOBOT_TOKEN  (необяз.) токен Crypto Pay из @CryptoBot -> автооплата криптой
  PANEL_BACKUP_HOURS (необяз.) как часто слать бэкап панели админу, по умолчанию 24, 0 = выкл
  DB_PATH          (необяз.) путь к SQLite-базе, по умолчанию bot.db
"""
import asyncio
import hashlib
import html
import json
import logging
import os
import re
import secrets
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
CRYPTOBOT_TOKEN = os.environ.get("CRYPTOBOT_TOKEN", "").strip()
PANEL_BACKUP_HOURS = env_int("PANEL_BACKUP_HOURS", 24)

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
CREATE TABLE IF NOT EXISTS gifts(
    code TEXT PRIMARY KEY, loc TEXT, devices INTEGER, months INTEGER, buyer INTEGER,
    redeemed_by INTEGER, created INTEGER, price INTEGER
);
"""

# добавление колонок в уже существующие базы (ошибка "duplicate column" игнорируется)
MIGRATIONS = [
    "ALTER TABLE users ADD COLUMN auto_renew INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE users ADD COLUMN plan_loc TEXT",
    "ALTER TABLE users ADD COLUMN plan_devices INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE users ADD COLUMN plan_months INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE users ADD COLUMN last_rotate INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE topups ADD COLUMN method TEXT DEFAULT 'manual'",
    "ALTER TABLE topups ADD COLUMN invoice_id TEXT",
]


class DB:
    def __init__(self, path: str):
        self.path = path
        self.lock = threading.Lock()
        self._open()

    def _open(self):
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        for stmt in MIGRATIONS:
            try:
                self.conn.execute(stmt)
            except sqlite3.OperationalError:
                pass
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

    async def raw(self, method: str, path: str) -> bytes:
        """Сырой ответ панели (например, файл базы)."""
        async with self.lock:
            if self.s is None:
                await self._login()
            headers = {}
            if self.token:
                headers["Authorization"] = f"Bearer {self.token}"
            elif self.csrf:
                headers["X-CSRF-Token"] = self.csrf
            async with self.s.request(method, self.url + path, headers=headers) as r:
                data = await r.read()
                status = r.status
        if status != 200:
            raise RuntimeError(f"Панель: HTTP {status} {method} {path}")
        return data


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
        [btn("🎁 Подарить подписку", "gift")],
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
        reply_markup=kb([[btn("🔄 Сменить ключ", f"rk:{loc.code}")]]),
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
    find = State()


# ---------------------------------------------------------------- меню
@dp.message(Command("start"))
async def start(m: Message, state: FSMContext, command: CommandObject):
    await state.clear()
    await m.answer(await menu_text(m.from_user.id, m.from_user.first_name), reply_markup=await main_kb(m.from_user.id))
    arg = command.args or ""
    if arg.startswith("gift_"):
        await m.answer(await redeem(m.from_user.id, arg[5:]))


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
            lines.extend(await usage_lines(loc, c))
    text = (
        f"👤 <b>Профиль</b>\n\n"
        f"ID: <code>{uid}</code>\n"
        f"💰 Баланс: <b>{u['balance']} ₽</b>\n"
    )
    if u["discount_pct"]:
        text += f"🎟 Скидка на следующую покупку: <b>{u['discount_pct']}%</b>\n"
    if u["auto_renew"] and u["plan_devices"]:
        text += f"🔁 Автопродление: <b>вкл</b> ({u['plan_devices']} устр., {u['plan_months']} мес., {final_price(u['plan_devices'], u['plan_months'])} ₽)\n"
    text += "\n<b>Подписки</b>\n" + ("\n".join(lines) if lines else "пока нет")
    text += f"\n\n👥 Приглашено: {refs}, заработано: {u['ref_earned']} ₽"
    ar_label = "🔁 Автопродление: вкл ✅" if u["auto_renew"] else "🔁 Автопродление: выкл"
    await show(cb, text, kb([[btn("💰 Пополнить", "topup"), btn("💳 Купить", "buy")], [btn(ar_label, "ar")], BACK]))


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
    await db.rc("UPDATE users SET plan_loc=?, plan_devices=?, plan_months=? WHERE id=?", code, n, m_, uid)
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
    amount = int(val)
    text, markup = method_screen(amount) if CRYPTOBOT_TOKEN else pay_screen(amount)
    await show(cb, text, markup)
    await cb.answer()


@dp.message(St.amount, F.text, ~F.text.startswith("/"))
async def top_custom(m: Message, state: FSMContext):
    raw = m.text.strip().replace(" ", "")
    if not raw.isdigit() or not MIN_TOPUP <= int(raw) <= 50000:
        return await m.answer(f"Нужно число от {MIN_TOPUP} до 50000. Попробуй ещё раз.")
    await state.clear()
    text, markup = method_screen(int(raw)) if CRYPTOBOT_TOKEN else pay_screen(int(raw))
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
    if code.startswith("GIFT-"):
        return await redeem_gift(uid, code)
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
    await apply_topup(t)
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
# ---------------------------------------------------------------- админка
def left_str(exp_ms) -> str:
    if not exp_ms:
        return "бессрочно"
    d = exp_ms - ms()
    if d <= 0:
        return f"истекла {fmt_date(exp_ms)}"
    days, rest = divmod(d, DAY_MS)
    hours = rest // 3600000
    if days:
        return f"{days} дн. {hours} ч."
    return f"{hours} ч. {(rest % 3600000) // 60000} мин."


def uid_from_email(loc: Location, email: str):
    m = re.fullmatch(r"tg(\d+)(?:-[a-z]+)?", email or "")
    if m and loc.email(int(m.group(1))) == email:
        return int(m.group(1))
    return None


async def all_users() -> dict:
    return {r["id"]: r for r in await db.all("SELECT id,username,full_name FROM users")}


def who(users: dict, uid) -> str:
    u = users.get(uid) or {}
    name = f"@{u['username']}" if u.get("username") else (u.get("full_name") or "—")
    return f"{html.escape(name)} (<code>{uid}</code>)"


def fmt_ip(x) -> str:
    if isinstance(x, dict):
        ip = x.get("ip") or x.get("IP") or str(x)
        ts = x.get("timestamp") or x.get("time")
        if isinstance(ts, (int, float)) and ts > 0:
            if ts < 1e11:
                ts *= 1000
            return html.escape(f"{ip} ({datetime.fromtimestamp(ts / 1000):%d.%m %H:%M})")
        return html.escape(str(ip))
    return html.escape(str(x))


async def resolve_user(q: str):
    q = (q or "").strip().lstrip("@")
    if not q:
        return None
    if q.isdigit():
        return int(q) if await get_user(int(q)) else None
    r = await db.one("SELECT id FROM users WHERE lower(username)=lower(?)", q)
    return r["id"] if r else None


ADM_BACK = [btn("⬅️ Админка", "adm:menu")]

ADMIN_COMMANDS = (
    "/user <code>ID или @ник</code> — карточка пользователя\n"
    "/online — кто онлайн · /expiring — скоро истекают · /subs — все подписки\n"
    "/addbal <code>ID сумма</code> — изменить баланс (можно минус)\n"
    "/give <code>ID дней [устройств]</code> — выдать подписку вручную\n"
    "/promo_add <code>КОД bonus|percent значение [лимит] [дней]</code>\n"
    "   например: /promo_add START50 bonus 50 100 30\n"
    "   или: /promo_add SALE20 percent 20\n"
    "/promo_list — список промокодов · /promo_del <code>КОД</code>\n"
    "/broadcast <code>текст</code> — рассылка всем\n"
    "/backup · /panelbackup — базы бота и панели\n"
    "Восстановление: отправь файл базы с подписью /restore"
)


def admin_kb():
    return kb([
        [btn("📊 Статистика", "adm:stats"), btn("🟢 Онлайн сейчас", "adm:online")],
        [btn("⏳ Скоро истекают", "adm:exp"), btn("📋 Все подписки", "adm:subs:0")],
        [btn("🔍 Найти пользователя", "adm:find")],
        [btn("🗄 Бэкап бота", "adm:bk"), btn("🗄 Бэкап панели", "adm:pbk")],
    ])


ADMIN_TEXT = "🛠 <b>Админ-панель</b>\n\nВыбери действие кнопкой. Остальные команды:\n\n" + ADMIN_COMMANDS


@dp.message(Command("admin"), IS_ADMIN)
async def admin_help(m: Message, state: FSMContext):
    await state.clear()
    await m.answer(ADMIN_TEXT, reply_markup=admin_kb())


@dp.callback_query(F.data == "adm:menu", IS_ADMIN)
async def adm_menu(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await show(cb, ADMIN_TEXT, admin_kb())
    await cb.answer()


async def stats_text() -> str:
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
            act = [c for c in cl if not c.get("expiryTime") or c["expiryTime"] > ms()]
            lines.append(f"{loc.title}: клиентов {len(cl)}, активных {len(act)}")
        except Exception as e:
            lines.append(f"{loc.title}: ошибка панели ({html.escape(str(e))})")
    return (
        f"📊 <b>Статистика</b>\n\nПользователей: {users} (+{new24} за сутки)\n"
        f"Пополнений всего: {income} ₽ (за 30 дн.: {income30} ₽)\n"
        f"Заявок в ожидании: {pend}\n\n" + "\n".join(lines)
    )


async def adm_online() -> str:
    users = await all_users()
    out = ["🟢 <b>Сейчас онлайн</b>\n"]
    for loc in avail():
        try:
            on = await loc.panel.call("POST", "panel/api/clients/onlines")
            cl = {c["email"]: c for c in await list_clients(loc)}
        except Exception as e:
            out.append(f"{loc.title}: ошибка панели ({html.escape(str(e))[:100]})")
            continue
        on = on if isinstance(on, list) else []
        out.append(f"<b>{loc.title}</b>: {len(on)} чел.")
        for email in on[:40]:
            uid = uid_from_email(loc, email)
            label = who(users, uid) if uid else html.escape(email)
            out.append(f"• {label} — {left_str(cl.get(email, {}).get('expiryTime'))}")
        if len(on) > 40:
            out.append(f"… и ещё {len(on) - 40}")
    return "\n".join(out)


async def adm_expiring(days: int = 7) -> str:
    users = await all_users()
    now = ms()
    soon, gone = [], []
    for loc in avail():
        try:
            cl = await list_clients(loc)
        except Exception as e:
            return f"{loc.title}: ошибка панели ({html.escape(str(e))[:100]})"
        for c in cl:
            exp = c.get("expiryTime") or 0
            if not exp:
                continue
            uid = uid_from_email(loc, c.get("email"))
            label = who(users, uid) if uid else html.escape(c.get("email") or "?")
            if now < exp < now + days * DAY_MS:
                soon.append((exp, f"• {label} — {left_str(exp)}"))
            elif now - 3 * DAY_MS < exp <= now:
                gone.append((exp, f"• {label} — {left_str(exp)}"))
    soon.sort()
    gone.sort(reverse=True)
    out = [f"⏳ <b>Истекают за {days} дн.</b> ({len(soon)})"]
    out += [x[1] for x in soon[:30]] or ["никого"]
    out += ["", f"❌ <b>Закончились за 3 дня</b> ({len(gone)})"]
    out += [x[1] for x in gone[:30]] or ["никого"]
    return "\n".join(out)


SUBS_PAGE = 15


async def adm_subs(page: int):
    users = await all_users()
    now = ms()
    rows = []
    for loc in avail():
        try:
            cl = await list_clients(loc)
        except Exception as e:
            return f"{loc.title}: ошибка панели ({html.escape(str(e))[:100]})", kb([ADM_BACK])
        for c in cl:
            exp = c.get("expiryTime") or 0
            if exp and exp <= now:
                continue
            uid = uid_from_email(loc, c.get("email"))
            label = who(users, uid) if uid else html.escape(c.get("email") or "?")
            rows.append((exp or 1 << 62, f"{label} — {left_str(exp)}, {c.get('limitIp') or '∞'} устр."))
    rows.sort()
    pages = max(1, (len(rows) + SUBS_PAGE - 1) // SUBS_PAGE)
    page = min(max(page, 0), pages - 1)
    chunk = rows[page * SUBS_PAGE:(page + 1) * SUBS_PAGE]
    text = f"📋 <b>Активные подписки</b> ({len(rows)}), стр. {page + 1}/{pages}\nСверху те, что скоро закончатся\n\n"
    text += "\n".join(f"{page * SUBS_PAGE + i + 1}. {r[1]}" for i, r in enumerate(chunk)) or "пока нет"
    nav = []
    if page > 0:
        nav.append(btn("◀️", f"adm:subs:{page - 1}"))
    if page < pages - 1:
        nav.append(btn("▶️", f"adm:subs:{page + 1}"))
    return text, kb(([nav] if nav else []) + [ADM_BACK])


async def user_card(uid: int):
    u = await get_user(uid)
    if not u:
        return None
    refs_n = (await db.one("SELECT COUNT(*) n FROM users WHERE ref_by=?", uid))["n"]
    spent = (await db.one("SELECT COALESCE(SUM(price),0) s FROM purchases WHERE uid=?", uid))["s"]
    name = f"@{u['username']}" if u["username"] else "—"
    lines = [
        f"👤 <b>{html.escape(u['full_name'] or '—')}</b> {html.escape(name)}",
        f"ID: <code>{uid}</code> · с {datetime.fromtimestamp(u['created'] or 0):%d.%m.%Y}",
        f"💰 Баланс: <b>{u['balance']} ₽</b>, потрачено: {spent} ₽",
        f"👥 Пригласил: {refs_n}, пришёл от: {u['ref_by'] or '—'}",
        f"🎟 Скидка: {u['discount_pct']}% · 🎁 Триал: {'использован' if u['trial_used'] else 'нет'}"
        f" · 🔁 Автопродление: {'вкл' if u['auto_renew'] else 'выкл'}",
    ]
    for loc in avail():
        try:
            c = await sub_of(uid, loc)
        except Exception as e:
            lines.append(f"\n{loc.title}: ошибка панели ({html.escape(str(e))[:100]})")
            continue
        if not c:
            lines.append(f"\n<b>{loc.title}</b>: подписки нет")
            continue
        exp = c.get("expiryTime") or 0
        active = not exp or exp > ms()
        lines.append(f"\n<b>{loc.title}</b> {'✅ активна' if active else '❌ закончилась'}")
        lines.append(f"   ⏳ {left_str(exp)}" + (f" (до {fmt_date(exp)})" if exp else ""))
        lines.append(f"   📱 Лимит устройств: {c.get('limitIp') or '∞'}")
        lines += await usage_lines(loc, c)
        try:
            ips = await loc.panel.call("POST", f"panel/api/clients/ips/{eq(c['email'])}")
            if isinstance(ips, list) and ips:
                lines.append("   🌐 IP-адреса:")
                lines += [f"      • {fmt_ip(x)}" for x in ips[:10]]
        except Exception:
            pass
    markup = kb([
        [btn("➕ 30 дней", f"adm:give:{uid}:30"), btn("🧹 Сбросить IP", f"adm:clrip:{uid}")],
        [btn("🔄 Обновить", f"adm:u:{uid}")],
        ADM_BACK,
    ])
    return "\n".join(lines), markup


@dp.message(Command("stats"), IS_ADMIN)
async def stats(m: Message):
    await m.answer(await stats_text(), reply_markup=kb([ADM_BACK]))


@dp.message(Command("online"), IS_ADMIN)
async def cmd_online(m: Message):
    await m.answer(await adm_online(), reply_markup=kb([[btn("🔄 Обновить", "adm:online")], ADM_BACK]))


@dp.message(Command("expiring"), IS_ADMIN)
async def cmd_expiring(m: Message, command: CommandObject):
    days = int(command.args) if (command.args or "").strip().isdigit() else 7
    await m.answer(await adm_expiring(days), reply_markup=kb([ADM_BACK]))


@dp.message(Command("subs"), IS_ADMIN)
async def cmd_subs(m: Message):
    text, markup = await adm_subs(0)
    await m.answer(text, reply_markup=markup)


@dp.message(Command("user"), IS_ADMIN)
async def user_info(m: Message, command: CommandObject):
    uid = await resolve_user(command.args or "")
    if not uid:
        return await m.answer("Не найден. Использование: /user ID или /user @ник")
    text, markup = await user_card(uid)
    await m.answer(text, reply_markup=markup)


@dp.callback_query(F.data == "adm:stats", IS_ADMIN)
async def adm_stats_cb(cb: CallbackQuery):
    await cb.answer()
    await show(cb, await stats_text(), kb([[btn("🔄 Обновить", "adm:stats")], ADM_BACK]))


@dp.callback_query(F.data == "adm:online", IS_ADMIN)
async def adm_online_cb(cb: CallbackQuery):
    await cb.answer()
    await show(cb, await adm_online(), kb([[btn("🔄 Обновить", "adm:online")], ADM_BACK]))


@dp.callback_query(F.data == "adm:exp", IS_ADMIN)
async def adm_exp_cb(cb: CallbackQuery):
    await cb.answer()
    await show(cb, await adm_expiring(7), kb([[btn("🔄 Обновить", "adm:exp")], ADM_BACK]))


@dp.callback_query(F.data.startswith("adm:subs:"), IS_ADMIN)
async def adm_subs_cb(cb: CallbackQuery):
    await cb.answer()
    text, markup = await adm_subs(int(cb.data.split(":")[2]))
    await show(cb, text, markup)


@dp.callback_query(F.data == "adm:find", IS_ADMIN)
async def adm_find(cb: CallbackQuery, state: FSMContext):
    await state.set_state(St.find)
    await show(cb, "🔍 Отправь ID или @ник пользователя.", kb([ADM_BACK]))
    await cb.answer()


@dp.message(St.find, F.text, ~F.text.startswith("/"), IS_ADMIN)
async def adm_find_in(m: Message, state: FSMContext):
    uid = await resolve_user(m.text)
    if not uid:
        return await m.answer("Не нашёл такого пользователя. Попробуй ещё раз или нажми «⬅️ Админка».", reply_markup=kb([ADM_BACK]))
    await state.clear()
    text, markup = await user_card(uid)
    await m.answer(text, reply_markup=markup)


@dp.callback_query(F.data.startswith("adm:u:"), IS_ADMIN)
async def adm_user_cb(cb: CallbackQuery):
    await cb.answer()
    card = await user_card(int(cb.data.split(":")[2]))
    if card:
        await show(cb, *card)


@dp.callback_query(F.data.startswith("adm:give:"), IS_ADMIN)
async def adm_give_cb(cb: CallbackQuery):
    _, _, uid, days = cb.data.split(":")
    uid, days = int(uid), int(days)
    loc = avail()[0]
    try:
        c = await sub_of(uid, loc)
        c = await grant(uid, loc, (c or {}).get("limitIp") or BASE_DEVICES, days)
    except Exception as e:
        logging.exception("admin give failed")
        return await cb.answer(f"Ошибка: {str(e)[:150]}", show_alert=True)
    try:
        await bot.send_message(uid, f"🎁 Администратор продлил вашу подписку на {days} дн. Действует до {fmt_date(c['expiryTime'])}.")
    except Exception:
        pass
    await cb.answer("Готово")
    card = await user_card(uid)
    if card:
        await show(cb, *card)


@dp.callback_query(F.data.startswith("adm:clrip:"), IS_ADMIN)
async def adm_clrip_cb(cb: CallbackQuery):
    uid = int(cb.data.split(":")[2])
    done = False
    for loc in avail():
        try:
            await loc.panel.call("POST", f"panel/api/clients/clearIps/{eq(loc.email(uid))}")
            done = True
        except Exception:
            logging.exception("clear ips failed")
    await cb.answer("IP-адреса сброшены" if done else "Не удалось сбросить", show_alert=not done)
    card = await user_card(uid)
    if card:
        await show(cb, *card)


@dp.callback_query(F.data == "adm:bk", IS_ADMIN)
async def adm_bk(cb: CallbackQuery):
    await cb.answer("Отправляю...")
    await send_backup(cb.from_user.id)


@dp.callback_query(F.data == "adm:pbk", IS_ADMIN)
async def adm_pbk(cb: CallbackQuery):
    await cb.answer("Отправляю...")
    try:
        if not await send_panel_backup(cb.from_user.id):
            await cb.message.answer("Не удалось отправить файл.")
    except Exception as e:
        await cb.message.answer(f"Ошибка: {html.escape(str(e))}")


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


# ---------------------------------------------------------------- новые возможности
def fmt_bytes(n) -> str:
    n = float(n or 0)
    for unit in ("Б", "КБ", "МБ", "ГБ", "ТБ"):
        if n < 1024 or unit == "ТБ":
            return f"{n:.0f} {unit}" if unit == "Б" else f"{n:.1f} {unit}"
        n /= 1024


async def usage_lines(loc: Location, c: dict) -> list:
    """Трафик, онлайн-статус и устройства. Любая ошибка просто пропускает строку."""
    out, email = [], c["email"]
    try:
        t = await loc.panel.call("GET", f"panel/api/clients/traffic/{eq(email)}")
        if isinstance(t, dict):
            used = (t.get("up") or 0) + (t.get("down") or 0)
            total = t.get("total") or 0
            out.append("   📊 Трафик: " + fmt_bytes(used) + (f" из {fmt_bytes(total)}" if total else " (без лимита)"))
    except Exception:
        pass
    try:
        on = await loc.panel.call("POST", "panel/api/clients/onlines")
        if isinstance(on, list):
            out.append("   🟢 Сейчас онлайн" if email in on else "   ⚪ Сейчас не в сети")
    except Exception:
        pass
    try:
        ips = await loc.panel.call("POST", f"panel/api/clients/ips/{eq(email)}")
        n = len(ips) if isinstance(ips, list) else 0
        out.append(f"   📱 Устройств (по IP): {n} из {c.get('limitIp') or '∞'}")
    except Exception:
        pass
    return out


# ---------- смена ключа
async def rotate_key(uid: int, loc: Location) -> dict:
    email = loc.email(uid)
    found = await get_client(loc, email)
    if not found:
        raise PanelError("подписка не найдена")
    old = found[0]
    new = {
        "email": email,
        "id": str(uuid.uuid4()),
        "flow": old.get("flow") or "",
        "limitIp": old.get("limitIp") or 1,
        "totalGB": 0,
        "expiryTime": old.get("expiryTime") or 0,
        "enable": True,
        "subId": uuid.uuid4().hex[:16],
    }
    await loc.panel.call("POST", f"panel/api/clients/del/{eq(email)}")
    try:
        await loc.panel.call("POST", "panel/api/clients/add", json={"inboundIds": [loc.inbound_id], "client": new})
    except Exception:
        back = dict(new)
        back["id"] = old.get("uuid") or old.get("id")
        back["subId"] = old.get("subId") or new["subId"]
        try:
            await loc.panel.call("POST", "panel/api/clients/add", json={"inboundIds": [loc.inbound_id], "client": back})
        except Exception:
            logging.exception("rollback after failed key rotation failed")
        raise
    return new


@dp.callback_query(F.data.startswith("rk:"))
async def rk_ask(cb: CallbackQuery):
    loc = LOCATIONS.get(cb.data.split(":")[1])
    if not loc or not loc.available:
        return await cb.answer("Локация недоступна", show_alert=True)
    u = await get_user(cb.from_user.id)
    if time.time() - (u["last_rotate"] or 0) < 3600:
        return await cb.answer("Ключ можно менять не чаще раза в час.", show_alert=True)
    await cb.message.answer(
        "🔄 <b>Сменить ключ?</b>\n\nСтарый ключ перестанет работать, срок подписки сохранится. "
        "Новый ключ придётся добавить в приложение на всех устройствах.",
        reply_markup=kb([[btn("✅ Да, сменить", f"rkc:{loc.code}")], [btn("✖ Отмена", "menu")]]),
    )
    await cb.answer()


@dp.callback_query(F.data.startswith("rkc:"))
async def rk_do(cb: CallbackQuery):
    loc = LOCATIONS.get(cb.data.split(":")[1])
    uid = cb.from_user.id
    if not loc or not loc.available:
        return await cb.answer("Локация недоступна", show_alert=True)
    if uid in busy:
        return await cb.answer("Подожди...")
    busy.add(uid)
    await cb.answer("Меняю ключ...")
    try:
        c = await rotate_key(uid, loc)
    except Exception as e:
        logging.exception("rotate failed")
        await show(cb, "😔 Не получилось сменить ключ. Подписка не пострадала, попробуй позже.", kb([BACK]))
        await notify_admins(f"⚠️ Ошибка смены ключа у {uid}: {html.escape(str(e))}")
        return
    finally:
        busy.discard(uid)
    await db.rc("UPDATE users SET last_rotate=? WHERE id=?", int(time.time()), uid)
    await show(cb, "✅ Ключ заменён. Старый больше не работает.", kb([BACK]))
    await send_key(uid, c, loc)


# ---------- автопродление
@dp.callback_query(F.data == "ar")
async def ar_toggle(cb: CallbackQuery):
    uid = cb.from_user.id
    u = await get_user(uid)
    if not u["auto_renew"] and (not u["plan_loc"] or not u["plan_devices"]):
        return await cb.answer("Автопродление запомнит твой тариф после первой покупки с баланса.", show_alert=True)
    await db.rc("UPDATE users SET auto_renew=? WHERE id=?", 0 if u["auto_renew"] else 1, uid)
    await profile(cb)


AUTORENEW_BEFORE_MS = 24 * 3600 * 1000


async def autorenew_tick():
    users = await db.all("SELECT * FROM users WHERE auto_renew=1 AND plan_devices>0 AND plan_loc IS NOT NULL")
    if not users:
        return
    now = ms()
    cache: dict = {}
    for u in users:
        loc = LOCATIONS.get(u["plan_loc"])
        if not loc or not loc.available or u["plan_months"] not in PERIODS:
            continue
        if loc.code not in cache:
            cache[loc.code] = {c["email"]: c for c in await list_clients(loc)}
        email = loc.email(u["id"])
        c = cache[loc.code].get(email)
        if not c:
            continue
        exp = c.get("expiryTime") or 0
        if exp == 0 or exp > now + AUTORENEW_BEFORE_MS or exp < now - 3 * DAY_MS:
            continue
        uid, n, m_ = u["id"], u["plan_devices"], u["plan_months"]
        cost = final_price(n, m_)
        if await db.rc("UPDATE users SET balance=balance-? WHERE id=? AND balance>=?", cost, uid, cost) == 0:
            if await db.rc("INSERT OR IGNORE INTO reminders(email,expiry) VALUES(?,?)", "ar-" + email, exp):
                try:
                    await bot.send_message(
                        uid,
                        f"🔁 Для автопродления не хватает денег: нужно <b>{cost} ₽</b>, на балансе {u['balance']} ₽.",
                        reply_markup=kb([[btn("💰 Пополнить", "topup")]]),
                    )
                except Exception:
                    pass
            continue
        try:
            c2 = await grant(uid, loc, n, 30 * m_)
        except Exception as e:
            logging.exception("autorenew failed")
            await db.rc("UPDATE users SET balance=balance+? WHERE id=?", cost, uid)
            if await db.rc("INSERT OR IGNORE INTO reminders(email,expiry) VALUES(?,?)", "arf-" + email, exp):
                await notify_admins(f"⚠️ Автопродление не удалось у {uid}: {html.escape(str(e))}")
            continue
        await db.ins(
            "INSERT INTO purchases(uid,loc,devices,months,price,created) VALUES(?,?,?,?,?,?)",
            uid, loc.code, n, m_, cost, int(time.time()),
        )
        try:
            await bot.send_message(
                uid, f"🔁 Автопродление: списано <b>{cost} ₽</b>, подписка действует до <b>{fmt_date(c2['expiryTime'])}</b>."
            )
        except Exception:
            pass


# ---------- подарочные подписки
@dp.callback_query(F.data == "gift")
async def gift_start(cb: CallbackQuery):
    rows = [[btn(f"{n} устр. — от {final_price(n, 1)} ₽", f"gd:{n}")] for n in range(BASE_DEVICES, MAX_DEVICES + 1)]
    rows.append(BACK)
    await show(
        cb,
        "🎁 <b>Подарить подписку</b>\n\nТы оплачиваешь с баланса и получаешь код и ссылку. "
        "Друг открывает ссылку (или вводит код в «🎟 Промокод») и получает ключ.\n\nСколько устройств?",
        kb(rows),
    )
    await cb.answer()


@dp.callback_query(F.data.startswith("gd:"))
async def gift_dev(cb: CallbackQuery):
    n = int(cb.data.split(":")[1])
    rows = [[btn(f"{m_} мес. — {final_price(n, m_)} ₽", f"gp:{n}:{m_}")] for m_ in PERIODS]
    rows.append([btn("⬅️ Назад", "gift")])
    await show(cb, f"🎁 Подарок · {n} устр.\n\n⏳ <b>На какой срок?</b>", kb(rows))
    await cb.answer()


@dp.callback_query(F.data.startswith("gp:"))
async def gift_confirm(cb: CallbackQuery):
    _, n, m_ = cb.data.split(":")
    n, m_ = int(n), int(m_)
    if m_ not in PERIODS or not BASE_DEVICES <= n <= MAX_DEVICES:
        return await cb.answer("Неверный заказ", show_alert=True)
    u = await get_user(cb.from_user.id)
    cost = final_price(n, m_)
    text = (
        f"🎁 <b>Подарок</b>\n\n📱 Устройств: {n}\n⏳ Срок: {m_} мес.\n\n"
        f"<b>К оплате: {cost} ₽</b>\n💰 Твой баланс: {u['balance']} ₽"
    )
    if u["balance"] >= cost:
        rows = [[btn(f"✅ Оплатить {cost} ₽", f"gb:{n}:{m_}")]]
    else:
        rows = [[btn(f"➕ Пополнить на {max(cost - u['balance'], MIN_TOPUP)} ₽", f"top:{max(cost - u['balance'], MIN_TOPUP)}")]]
    rows.append([btn("⬅️ Назад", f"gd:{n}")])
    await show(cb, text, kb(rows))
    await cb.answer()


@dp.callback_query(F.data.startswith("gb:"))
async def gift_buy(cb: CallbackQuery):
    _, n, m_ = cb.data.split(":")
    n, m_ = int(n), int(m_)
    uid = cb.from_user.id
    if m_ not in PERIODS or not BASE_DEVICES <= n <= MAX_DEVICES:
        return await cb.answer("Неверный заказ", show_alert=True)
    cost = final_price(n, m_)
    if await db.rc("UPDATE users SET balance=balance-? WHERE id=? AND balance>=?", cost, uid, cost) == 0:
        return await cb.answer("Недостаточно средств", show_alert=True)
    code = "GIFT-" + secrets.token_hex(4).upper()
    await db.rc(
        "INSERT INTO gifts(code,loc,devices,months,buyer,created,price) VALUES(?,?,?,?,?,?,?)",
        code, avail()[0].code, n, m_, uid, int(time.time()), cost,
    )
    link = f"https://t.me/{BOT_USERNAME}?start=gift_{code}"
    await show(
        cb,
        f"🎁 <b>Подарок готов!</b> Списано {cost} ₽.\n\nКод: <code>{code}</code>\n"
        f"Ссылка для друга:\n{link}\n\nПодписка: {n} устр., {m_} мес. Код одноразовый.",
        kb([BACK]),
    )
    await cb.answer()


async def redeem_gift(uid: int, code: str) -> str:
    g = await db.one("SELECT * FROM gifts WHERE code=?", code)
    if not g:
        return "❌ Такого кода нет."
    if g["redeemed_by"]:
        return "😕 Этот подарочный код уже использован."
    loc = LOCATIONS.get(g["loc"])
    if not loc or not loc.available:
        return "Локация временно недоступна, напиши в поддержку."
    if await db.rc("UPDATE gifts SET redeemed_by=? WHERE code=? AND redeemed_by IS NULL", uid, code) == 0:
        return "😕 Этот подарочный код уже использован."
    try:
        c = await grant(uid, loc, g["devices"], 30 * g["months"])
    except Exception as e:
        logging.exception("gift grant failed")
        await db.rc("UPDATE gifts SET redeemed_by=NULL WHERE code=?", code)
        await notify_admins(f"⚠️ Ошибка активации подарка {code} у {uid}: {html.escape(str(e))}")
        return "Не получилось активировать подарок. Попробуй позже."
    await send_key(uid, c, loc)
    if g["buyer"] and g["buyer"] != uid:
        try:
            await bot.send_message(g["buyer"], "🎁 Твой подарок активирован. Спасибо, что ты с нами!")
        except Exception:
            pass
    return f"🎁 Подарок активирован! Подписка действует до <b>{fmt_date(c['expiryTime'])}</b>. Ключ выше."


# ---------- пополнение: автооплата CryptoBot
async def cryptopay(method: str, params: dict | None = None) -> dict:
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as s:
        async with s.post(
            f"https://pay.crypt.bot/api/{method}",
            headers={"Crypto-Pay-API-Token": CRYPTOBOT_TOKEN},
            json=params or {},
        ) as r:
            j = await r.json(content_type=None)
    if not isinstance(j, dict) or not j.get("ok"):
        err = j.get("error") if isinstance(j, dict) else j
        raise RuntimeError(f"CryptoBot: {err}")
    return j["result"]


def method_screen(amount: int):
    text = f"💰 <b>Пополнение на {amount} ₽</b>\n\nВыбери способ оплаты:"
    rows = [
        [btn("💎 Криптой: баланс пополнится сам", f"cry:{amount}")],
        [btn("🏦 Переводом: проверит админ", f"man:{amount}")],
        [btn("✖ Отмена", "cancel")],
    ]
    return text, kb(rows)


@dp.callback_query(F.data.startswith("man:"))
async def manual_pay(cb: CallbackQuery):
    text, markup = pay_screen(int(cb.data.split(":")[1]))
    await show(cb, text, markup)
    await cb.answer()


@dp.callback_query(F.data.startswith("cry:"))
async def crypto_pay(cb: CallbackQuery):
    if not CRYPTOBOT_TOKEN:
        return await cb.answer("Оплата криптой не настроена", show_alert=True)
    amount = int(cb.data.split(":")[1])
    uid = cb.from_user.id
    open_n = (await db.one(
        "SELECT COUNT(*) n FROM topups WHERE uid=? AND status='invoice' AND created>?", uid, int(time.time()) - 3600
    ))["n"]
    if open_n >= 5:
        return await cb.answer("Слишком много открытых счетов, подожди час.", show_alert=True)
    tid = await db.ins(
        "INSERT INTO topups(uid,amount,status,created,method) VALUES(?,?,?,?,?)",
        uid, amount, "invoice", int(time.time()), "crypto",
    )
    try:
        inv = await cryptopay("createInvoice", {
            "currency_type": "fiat", "fiat": "RUB", "amount": str(amount),
            "description": f"Пополнение баланса на {amount} RUB",
            "payload": str(tid), "expires_in": 3600,
            "allow_comments": False, "allow_anonymous": False,
        })
    except Exception as e:
        logging.exception("createInvoice failed")
        await db.rc("UPDATE topups SET status='failed' WHERE id=?", tid)
        await notify_admins(f"⚠️ Не удалось создать счёт CryptoBot: {html.escape(str(e))}")
        return await show(cb, "😔 Не получилось создать счёт. Попробуй перевод вручную или позже.", kb([BACK]))
    await db.rc("UPDATE topups SET invoice_id=? WHERE id=?", str(inv["invoice_id"]), tid)
    await show(
        cb,
        f"💎 <b>Счёт на {amount} ₽</b>\n\nОплатите в CryptoBot любой криптовалютой. "
        "Баланс пополнится автоматически в течение минуты после оплаты. Счёт действует час.",
        kb([
            [btn("💎 Оплатить", url=inv["bot_invoice_url"])],
            [btn("🔄 Проверить оплату", f"chk:{tid}")],
            BACK,
        ]),
    )
    await cb.answer()


async def check_invoices(rows: list):
    ids = ",".join(r["invoice_id"] for r in rows)
    res = await cryptopay("getInvoices", {"invoice_ids": ids, "count": 100})
    items = res.get("items") if isinstance(res, dict) else res
    status = {str(i.get("invoice_id")): i.get("status") for i in (items or [])}
    for r in rows:
        s = status.get(r["invoice_id"])
        if s == "paid":
            if await db.rc("UPDATE topups SET status='ok' WHERE id=? AND status='invoice'", r["id"]) == 1:
                await apply_topup(r)
                await notify_admins(f"💎 Автопополнение: пользователь <code>{r['uid']}</code> +{r['amount']} ₽")
        elif s == "expired" or (s is None and time.time() - r["created"] > 7200):
            await db.rc("UPDATE topups SET status='expired' WHERE id=? AND status='invoice'", r["id"])


@dp.callback_query(F.data.startswith("chk:"))
async def chk_invoice(cb: CallbackQuery):
    t = await db.one("SELECT * FROM topups WHERE id=?", int(cb.data.split(":")[1]))
    if not t or t["uid"] != cb.from_user.id:
        return await cb.answer("Счёт не найден", show_alert=True)
    if t["status"] == "invoice" and t["invoice_id"]:
        try:
            await check_invoices([t])
        except Exception:
            logging.exception("check invoice failed")
        t = await db.one("SELECT * FROM topups WHERE id=?", t["id"])
    if t["status"] == "ok":
        return await cb.answer("✅ Оплата получена, баланс пополнен!", show_alert=True)
    if t["status"] in ("expired", "failed"):
        return await cb.answer("Счёт истёк. Создай новый.", show_alert=True)
    await cb.answer("Оплата ещё не поступила. Подожди минуту и нажми снова.", show_alert=True)


async def crypto_watcher():
    while True:
        await asyncio.sleep(20)
        try:
            rows = await db.all("SELECT * FROM topups WHERE status='invoice' AND invoice_id IS NOT NULL")
            if rows:
                await check_invoices(rows)
        except Exception:
            logging.exception("crypto watcher failed")


async def apply_topup(t: dict):
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


# ---------- бэкап базы панели 3x-ui
async def send_panel_backup(to: int | None = None):
    sent = 0
    for loc in avail():
        data = await loc.panel.raw("GET", "panel/api/server/getDb")
        ext = "db" if data.startswith(b"SQLite format 3") else "dump"
        f = BufferedInputFile(data, filename=f"panel_{loc.code}_{datetime.now():%Y%m%d_%H%M}.{ext}")
        cap = (f"🗄 Бэкап панели 3x-ui ({loc.title}). Здесь клиенты и сроки подписок. "
               "Восстановление: панель → Резервное копирование → Импорт.")
        for a in ([to] if to else ADMINS):
            try:
                await bot.send_document(a, f, caption=cap)
                sent += 1
            except Exception:
                logging.exception("panel backup send failed")
    return sent


@dp.message(Command("panelbackup"), IS_ADMIN)
async def panelbackup(m: Message):
    try:
        n = await send_panel_backup(m.from_user.id)
        if not n:
            await m.answer("Не удалось отправить файл.")
    except Exception as e:
        await m.answer(f"Ошибка: {html.escape(str(e))}")


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
    last_db = 0.0
    last_panel = 0.0
    while True:
        now = time.time()
        try:
            await autorenew_tick()
        except Exception:
            logging.exception("autorenew failed")
        try:
            await remind_expiring()
        except Exception:
            logging.exception("remind failed")
        if now - last_db >= 6 * 3600:
            last_db = now
            try:
                if (await db.one("SELECT COUNT(*) n FROM users"))["n"] > 0:
                    data = await asyncio.to_thread(db.dump)
                    h = hashlib.md5(data).hexdigest()
                    if h != last_hash:
                        last_hash = await send_backup(caption="🗄 Автобэкап базы")
            except Exception:
                logging.exception("backup failed")
        if PANEL_BACKUP_HOURS and now - last_panel >= PANEL_BACKUP_HOURS * 3600:
            last_panel = now
            try:
                await send_panel_backup()
            except Exception:
                logging.exception("panel backup failed")
        await asyncio.sleep(1800)


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
    if CRYPTOBOT_TOKEN:
        asyncio.create_task(crypto_watcher())
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
