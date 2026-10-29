"""
Бот для продажи VPN-ключей через панель 3x-ui.

Покупатель выбирает число устройств -> переводит деньги -> жмёт "Я оплатил" ->
админу приходит запрос -> админ жмёт "Выдать" -> бот создаёт клиента в 3x-ui
и присылает покупателю ссылку vless://.

Покупатели хранятся прямо в панели 3x-ui (email = tg<ID>), отдельная база не нужна.
Рассчитан на inbound VLESS + Reality (TCP).

Переменные окружения:
    BOT_TOKEN    токен НОВОГО бота от @BotFather (для покупателей)
    ADMIN_IDS    твой Telegram ID (можно несколько через запятую)
    PANEL_URL    адрес панели целиком, как прислал установщик: http://IP:порт/путь/
    PANEL_USER   логин панели
    PANEL_PASS   пароль панели
    INBOUND_ID   номер inbound (VLESS Reality) в панели
    SERVER_HOST  IP или домен сервера для ссылки (обычно тот же IP)
    PAY_INFO     текст с реквизитами для оплаты (карта, СБП и т.д.)
"""
import asyncio
import html
import json
import logging
import os
import time
import uuid
from datetime import datetime
from urllib.parse import quote

import aiohttp
from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

TOKEN = os.environ["BOT_TOKEN"]
ADMINS = {int(x) for x in os.environ.get("ADMIN_IDS", "").split(",") if x.strip()}
PANEL = os.environ["PANEL_URL"].rstrip("/") + "/"
PANEL_USER = os.environ["PANEL_USER"]
PANEL_PASS = os.environ["PANEL_PASS"]
INBOUND_ID = int(os.environ["INBOUND_ID"])
HOST = os.environ["SERVER_HOST"]
PAY_INFO = os.environ.get("PAY_INFO", "Реквизиты уточни у администратора.")

BASE_PRICE = 150      # ₽ за 30 дней
BASE_DEVICES = 2      # устройств в базовом тарифе
EXTRA_PRICE = 50      # ₽ за каждое доп. устройство
DAYS = 30
MAX_DEVICES = 5
DAY_MS = 86400 * 1000

bot = Bot(TOKEN)
dp = Dispatcher()


def price(devices: int) -> int:
    return BASE_PRICE + max(0, devices - BASE_DEVICES) * EXTRA_PRICE


# ---------------------------------------------------------------- панель 3x-ui
class Panel:
    def __init__(self):
        self.s: aiohttp.ClientSession | None = None
        self.lock = asyncio.Lock()

    async def _login(self):
        if self.s:
            await self.s.close()
        self.s = aiohttp.ClientSession(
            cookie_jar=aiohttp.CookieJar(unsafe=True),
            timeout=aiohttp.ClientTimeout(total=30),
        )
        async with self.s.post(
            PANEL + "login", data={"username": PANEL_USER, "password": PANEL_PASS}
        ) as r:
            j = await r.json(content_type=None)
        if not j.get("success"):
            raise RuntimeError("Панель: неверный логин или пароль")

    async def call(self, method: str, path: str, **kw):
        async with self.lock:
            for attempt in (0, 1):
                if self.s is None or attempt == 1:
                    await self._login()
                try:
                    async with self.s.request(method, PANEL + path, **kw) as r:
                        j = await r.json(content_type=None)
                except ValueError:  # сессия протухла -> вернулась html/404
                    continue
                if not j.get("success"):
                    raise RuntimeError(f"Панель: {j.get('msg')}")
                return j.get("obj")
            raise RuntimeError("Панель не отвечает")


panel = Panel()


async def get_inbound() -> dict:
    return await panel.call("GET", f"panel/api/inbounds/get/{INBOUND_ID}")


def clients_of(inb: dict) -> list:
    return json.loads(inb["settings"]).get("clients", [])


def find_client(inb: dict, email: str):
    for c in clients_of(inb):
        if c.get("email") == email:
            return c
    return None


def make_link(inb: dict, c: dict) -> str:
    st = json.loads(inb["streamSettings"])
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
    return f"vless://{c['id']}@{HOST}:{inb['port']}?{query}#{quote('VPN-' + c['email'])}"


async def grant(uid: int, devices: int):
    """Создаёт или продлевает клиента на 30 дней. Возвращает (client, inbound)."""
    email = f"tg{uid}"
    inb = await get_inbound()
    c = find_client(inb, email)
    now = int(time.time() * 1000)

    if c:
        c["expiryTime"] = max(now, c.get("expiryTime") or 0) + DAYS * DAY_MS
        c["limitIp"] = devices
        c["enable"] = True
        c["totalGB"] = 0
        await panel.call(
            "POST",
            f"panel/api/inbounds/updateClient/{c['id']}",
            data={"id": INBOUND_ID, "settings": json.dumps({"clients": [c]})},
        )
    else:
        st = json.loads(inb["streamSettings"])
        vision = st.get("network", "tcp") == "tcp" and st.get("security") in ("reality", "tls")
        c = {
            "id": str(uuid.uuid4()),
            "flow": "xtls-rprx-vision" if vision else "",
            "email": email,
            "limitIp": devices,
            "totalGB": 0,
            "expiryTime": now + DAYS * DAY_MS,
            "enable": True,
            "tgId": str(uid),
            "subId": uuid.uuid4().hex[:16],
            "comment": "",
            "reset": 0,
        }
        await panel.call(
            "POST",
            "panel/api/inbounds/addClient",
            data={"id": INBOUND_ID, "settings": json.dumps({"clients": [c]})},
        )
    return c, inb


def fmt_date(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000).strftime("%d.%m.%Y")


# ---------------------------------------------------------------- клавиатуры
def main_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="💳 Купить / продлить", callback_data="buy")],
            [InlineKeyboardButton(text="🔑 Мой ключ", callback_data="key")],
        ]
    )


def devices_kb() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=f"{n} устр. — {price(n)} ₽ / {DAYS} дн.", callback_data=f"buy:{n}")]
        for n in range(BASE_DEVICES, MAX_DEVICES + 1)
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ---------------------------------------------------------------- покупатель
@dp.message(Command("start"))
async def start(m: Message):
    await m.answer(
        f"Привет! VPN без ограничения трафика.\n\n"
        f"{BASE_PRICE} ₽ за {DAYS} дней на {BASE_DEVICES} устройства, "
        f"каждое дополнительное +{EXTRA_PRICE} ₽.",
        reply_markup=main_kb(),
    )


@dp.callback_query(F.data == "buy")
async def buy(cb: CallbackQuery):
    await cb.message.answer("Сколько устройств подключить?", reply_markup=devices_kb())
    await cb.answer()


@dp.callback_query(F.data.startswith("buy:"))
async def buy_n(cb: CallbackQuery):
    n = int(cb.data.split(":")[1])
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="✅ Я оплатил", callback_data=f"paid:{n}")]]
    )
    await cb.message.answer(
        f"К оплате: <b>{price(n)} ₽</b> ({n} устр., {DAYS} дней)\n\n"
        f"{html.escape(PAY_INFO)}\n\nПосле перевода нажми кнопку.",
        parse_mode="HTML",
        reply_markup=kb,
    )
    await cb.answer()


@dp.callback_query(F.data.startswith("paid:"))
async def paid(cb: CallbackQuery):
    n = int(cb.data.split(":")[1])
    u = cb.from_user
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Выдать", callback_data=f"ok:{u.id}:{n}"),
                InlineKeyboardButton(text="❌ Отклонить", callback_data=f"no:{u.id}"),
            ]
        ]
    )
    name = f"@{u.username}" if u.username else u.full_name
    for admin in ADMINS:
        try:
            await bot.send_message(
                admin,
                f"Оплата от {html.escape(name)} (id <code>{u.id}</code>)\n"
                f"{n} устр. — {price(n)} ₽",
                parse_mode="HTML",
                reply_markup=kb,
            )
        except Exception:
            logging.exception("не смог написать админу %s", admin)
    await cb.message.edit_reply_markup(reply_markup=None)
    await cb.message.answer("Принято. Как только оплату проверят, пришлю ключ.")
    await cb.answer()


async def send_key(uid: int, c: dict, inb: dict):
    link = make_link(inb, c)
    await bot.send_message(
        uid,
        f"Готово. Действует до <b>{fmt_date(c['expiryTime'])}</b>, устройств: {c['limitIp']}.\n\n"
        f"Твой ключ (нажми, чтобы скопировать):\n<code>{html.escape(link)}</code>\n\n"
        "Приложения: v2rayNG (Android), Streisand или Happ (iPhone), "
        "v2rayN / Hiddify (ПК). Добавь ключ из буфера обмена.",
        parse_mode="HTML",
    )


@dp.callback_query(F.data == "key")
async def my_key(cb: CallbackQuery):
    await cb.answer()
    try:
        inb = await get_inbound()
        c = find_client(inb, f"tg{cb.from_user.id}")
    except Exception as e:
        logging.exception("panel error")
        return await cb.message.answer(f"Не получилось связаться с сервером: {e}")
    if not c:
        return await cb.message.answer("Подписки пока нет.", reply_markup=main_kb())
    if (c.get("expiryTime") or 0) < int(time.time() * 1000):
        return await cb.message.answer(
            f"Подписка закончилась {fmt_date(c['expiryTime'])}. Продли её.", reply_markup=main_kb()
        )
    await send_key(cb.from_user.id, c, inb)


# ---------------------------------------------------------------- админ
@dp.callback_query(F.data.startswith("ok:"))
async def approve(cb: CallbackQuery):
    if cb.from_user.id not in ADMINS:
        return await cb.answer("Нет доступа", show_alert=True)
    _, uid, n = cb.data.split(":")
    uid, n = int(uid), int(n)
    await cb.answer("Выдаю...")
    try:
        c, inb = await grant(uid, n)
        await send_key(uid, c, inb)
    except Exception as e:
        logging.exception("grant failed")
        return await cb.message.answer(f"Ошибка выдачи: {e}")
    await cb.message.edit_text(f"{cb.message.text}\n\n✅ Выдано")


@dp.callback_query(F.data.startswith("no:"))
async def reject(cb: CallbackQuery):
    if cb.from_user.id not in ADMINS:
        return await cb.answer("Нет доступа", show_alert=True)
    uid = int(cb.data.split(":")[1])
    try:
        await bot.send_message(uid, "Оплата не подтверждена. Если это ошибка, напиши администратору.")
    except Exception:
        pass
    await cb.message.edit_text(f"{cb.message.text}\n\n❌ Отклонено")
    await cb.answer()


@dp.message(Command("stats"))
async def stats(m: Message):
    if m.from_user.id not in ADMINS:
        return
    inb = await get_inbound()
    now = int(time.time() * 1000)
    cl = clients_of(inb)
    active = [c for c in cl if (c.get("expiryTime") or 0) > now]
    await m.answer(f"Всего клиентов: {len(cl)}\nАктивных: {len(active)}")


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
    logging.basicConfig(level=logging.INFO)
    await start_web()
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
