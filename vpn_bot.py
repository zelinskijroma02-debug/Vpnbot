"""
Бот для продажи VPN-ключей через панель 3x-ui.

Покупатель выбирает число устройств -> переводит деньги -> жмёт "Я оплатил" ->
админу приходит запрос -> админ жмёт "Выдать" -> бот создаёт клиента в 3x-ui
и присылает покупателю ссылку vless://.

Покупатели хранятся прямо в панели 3x-ui (email = tg<ID>), отдельная база не нужна.
Рассчитан на inbound VLESS + Reality (TCP).

Команды админа: /stats, /backup (файл базы), /migrate и /switch (смена сервера).

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
    BufferedInputFile,
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
def _s(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    return str(v)


class Panel:
    def __init__(self, base: str, user: str, pw: str):
        self.base = base.rstrip("/") + "/"
        self.user = user
        self.pw = pw
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
            self.base + "login", data={"username": self.user, "password": self.pw}
        ) as r:
            j = await r.json(content_type=None)
        if not j.get("success"):
            raise RuntimeError("Панель: неверный логин или пароль")

    async def call(self, method: str, path: str, raw: bool = False, **kw):
        if isinstance(kw.get("data"), dict):
            kw["data"] = {k: _s(v) for k, v in kw["data"].items()}
        async with self.lock:
            for attempt in (0, 1):
                if self.s is None or attempt == 1:
                    await self._login()
                try:
                    async with self.s.request(method, self.base + path, **kw) as r:
                        if raw:
                            body = await r.read()
                            if body.startswith(b"SQLite format 3"):
                                return body
                            continue
                        j = await r.json(content_type=None)
                except ValueError:  # сессия протухла -> вернулась html/404
                    continue
                if not j.get("success"):
                    raise RuntimeError(f"Панель: {j.get('msg')}")
                return j.get("obj")
            raise RuntimeError("Панель не отвечает")


panel = Panel(PANEL, PANEL_USER, PANEL_PASS)


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


async def ensure_access_log() -> bool:
    """Включает access-лог xray: без него лимит устройств не работает.
    Возвращает True, если пришлось изменить настройку."""
    raw = await panel.call("POST", "panel/xray/")
    data = json.loads(raw) if isinstance(raw, str) else raw
    xs = data["xraySetting"]
    cfg = json.loads(xs) if isinstance(xs, str) else xs
    log = cfg.setdefault("log", {})
    if log.get("access") not in (None, "", "none"):
        return False
    log["access"] = "./access.log"
    form = {"xraySetting": json.dumps(cfg)}
    if data.get("outboundTestUrl"):
        form["outboundTestUrl"] = data["outboundTestUrl"]
    await panel.call("POST", "panel/xray/update", data=form)
    return True


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


async def send_key(uid: int, c: dict, inb: dict, intro: str = "Готово."):
    link = make_link(inb, c)
    await bot.send_message(
        uid,
        f"{intro} Действует до <b>{fmt_date(c['expiryTime'])}</b>, устройств: {c['limitIp']}.\n\n"
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
        bg(send_backup("после выдачи ключа"))
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


# ---------------------------------------------------------------- бэкап и смена сервера
_tasks: set = set()


def bg(coro):
    """Запускает фоновую задачу и держит на неё ссылку."""
    t = asyncio.create_task(coro)
    _tasks.add(t)
    t.add_done_callback(_tasks.discard)


async def send_backup(reason: str = ""):
    """Шлёт админам файл базы панели (все клиенты, сроки, ключи)."""
    data = await panel.call("GET", "panel/api/server/getDb", raw=True)
    name = f"x-ui-{datetime.now().strftime('%Y%m%d-%H%M')}.db"
    for admin in ADMINS:
        try:
            await bot.send_document(
                admin, BufferedInputFile(data, filename=name), caption=f"Бэкап панели {reason}".strip()
            )
        except Exception:
            logging.exception("бэкап не отправился %s", admin)


async def backup_loop():
    while True:
        await asyncio.sleep(24 * 3600)
        try:
            await send_backup("(ежедневный)")
        except Exception:
            logging.exception("ежедневный бэкап не удался")


async def copy_inbound_to(new: Panel) -> int:
    """Копирует inbound (порт, ключи Reality, всех клиентов) в новую панель."""
    inb = await get_inbound()
    keys = ("remark", "enable", "expiryTime", "listen", "port", "protocol",
            "settings", "streamSettings", "sniffing", "allocate", "total", "up", "down")
    form = {k: inb[k] for k in keys if k in inb}
    res = await new.call("POST", "panel/api/inbounds/add", data=form)
    return res["id"]


async def switch_to(new: Panel, host: str, inbound_id: int) -> int:
    """Переключает бота на новый сервер и рассылает клиентам новые ключи."""
    global panel, HOST, INBOUND_ID
    panel, HOST, INBOUND_ID = new, host, inbound_id
    try:
        await ensure_access_log()
    except Exception:
        logging.exception("access log на новой панели не включился")
    inb = await get_inbound()
    now = int(time.time() * 1000)
    sent = 0
    for c in clients_of(inb):
        exp = c.get("expiryTime") or 0
        if not c.get("tgId") or (exp and exp < now):
            continue
        try:
            await send_key(
                int(c["tgId"]), c, inb,
                intro="Сервер обновлён, старый ключ больше не работает. Вот новый.",
            )
            sent += 1
        except Exception:
            logging.exception("не смог отправить ключ %s", c.get("tgId"))
        await asyncio.sleep(0.1)
    return sent


def env_hint(url: str, host: str, inbound_id: int) -> str:
    return (
        "Чтобы бот не откатился на старый сервер после перезапуска, "
        "в Render → Environment поставь:\n"
        f"SERVER_HOST={host}\nPANEL_URL={url}\nINBOUND_ID={inbound_id}\n"
        "PANEL_USER и PANEL_PASS: логин и пароль новой панели."
    )


@dp.message(Command("backup"))
async def backup_cmd(m: Message):
    if m.from_user.id not in ADMINS:
        return
    try:
        await send_backup("(по запросу)")
    except Exception as e:
        await m.answer(f"Не получилось: {e}")


@dp.message(Command("migrate"))
async def migrate_cmd(m: Message):
    if m.from_user.id not in ADMINS:
        return
    parts = (m.text or "").split()
    if len(parts) != 5:
        return await m.answer(
            "Формат:\n/migrate АДРЕС_НОВОЙ_ПАНЕЛИ ЛОГИН ПАРОЛЬ НОВЫЙ_IP\n\n"
            "Адрес, логин и пароль новой панели пришлёт бот-установщик."
        )
    _, url, user, pw, host = parts
    try:
        await m.delete()
    except Exception:
        pass
    status = await m.answer("Переношу клиентов на новый сервер...")
    try:
        await send_backup("(перед переносом)")
    except Exception:
        logging.exception("бэкап перед переносом не удался")
    try:
        new = Panel(url, user, pw)
        new_id = await copy_inbound_to(new)
        sent = await switch_to(new, host, new_id)
    except Exception as e:
        logging.exception("migrate failed")
        return await status.edit_text(
            f"Не получилось: {e}\n\nЕсли старая панель недоступна: открой новую панель → "
            "Настройки → Резервная копия → Импорт, загрузи последний файл бэкапа "
            "и потом напиши /switch АДРЕС ЛОГИН ПАРОЛЬ IP"
        )
    await status.edit_text(f"Готово. Новые ключи получили клиентов: {sent}.\n\n{env_hint(url, host, new_id)}")


@dp.message(Command("switch"))
async def switch_cmd(m: Message):
    """Переключить бота на новую панель, где база уже восстановлена из бэкапа."""
    if m.from_user.id not in ADMINS:
        return
    parts = (m.text or "").split()
    if len(parts) != 5:
        return await m.answer("Формат:\n/switch АДРЕС_ПАНЕЛИ ЛОГИН ПАРОЛЬ НОВЫЙ_IP")
    _, url, user, pw, host = parts
    try:
        await m.delete()
    except Exception:
        pass
    try:
        new = Panel(url, user, pw)
        inbounds = await new.call("GET", "panel/api/inbounds/list")
        vless = [i for i in inbounds if i.get("protocol") == "vless"]
        if not vless:
            raise RuntimeError("в новой панели нет inbound VLESS")
        sent = await switch_to(new, host, vless[0]["id"])
    except Exception as e:
        logging.exception("switch failed")
        return await m.answer(f"Не получилось: {e}")
    await m.answer(f"Готово. Новые ключи получили клиентов: {sent}.\n\n{env_hint(url, host, vless[0]['id'])}")


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


async def startup_tasks():
    try:
        if await ensure_access_log():
            logging.info("access log xray включён")
    except Exception as e:
        logging.exception("access log не включился")
        for admin in ADMINS:
            try:
                await bot.send_message(
                    admin,
                    f"Не смог включить access-лог xray ({e}). Без него лимит устройств не работает. "
                    "Включи вручную: панель → Xray Configs → Log → Access Log = ./access.log",
                )
            except Exception:
                pass
    await backup_loop()


async def main():
    logging.basicConfig(level=logging.INFO)
    await start_web()
    bg(startup_tasks())
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
