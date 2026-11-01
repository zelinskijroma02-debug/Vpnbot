#!/usr/bin/env bash
set -e
RAW="https://raw.githubusercontent.com/zelinskijroma02-debug/Vpnbot/main/vpn_bot.py"
APP=/opt/vpnbot
ask() { read -r -p "$2: " v < /dev/tty; printf -v "$1" '%s' "$v"; }
ask BOT_TOKEN "1/3 Токен бота ПРОДАЖ (@BotFather, /mybots, API Token)"
ask ADMIN_ID "2/3 Твой Telegram ID (цифры)"
ask PAY_INFO "3/3 Реквизиты для оплаты (одной строкой)"
PAY_INFO="${PAY_INFO//\"/}"

echo "== Ставлю пакеты"
export DEBIAN_FRONTEND=noninteractive
apt-get update -y >/dev/null
apt-get install -y python3-venv python3-pip curl fail2ban >/dev/null

echo "== Читаю настройки панели из восстановленной базы"
read -r PORT BPATH INB <<<"$(python3 - <<'PY'
import sqlite3
c = sqlite3.connect('/etc/x-ui/x-ui.db')
g = lambda k: c.execute("select value from settings where key=?", (k,)).fetchone()[0]
i = c.execute("select id from inbounds where protocol='vless' order by id limit 1").fetchone()
print(g('webPort'), g('webBasePath'), i[0] if i else '')
PY
)"
[ -n "$INB" ] || { echo "ОШИБКА: в панели нет inbound VLESS"; exit 1; }

echo "== Задаю новый логин и пароль панели"
PUSER="$(tr -dc 'A-Za-z0-9' < /dev/urandom | head -c 8)"
PPASS="$(tr -dc 'A-Za-z0-9' < /dev/urandom | head -c 16)"
/usr/local/x-ui/x-ui setting -username "$PUSER" -password "$PPASS" >/dev/null
systemctl restart x-ui
IP="$(curl -4 -fsS -m 10 https://api.ipify.org)"
URL="http://127.0.0.1:${PORT}${BPATH}"
for _ in $(seq 1 30); do curl -fsS -m 3 -o /dev/null "$URL" && break; sleep 1; done
printf 'Панель: http://%s:%s%s\nЛогин: %s\nПароль: %s\n' "$IP" "$PORT" "$BPATH" "$PUSER" "$PPASS" > /root/panel_info.txt
chmod 600 /root/panel_info.txt

echo "== Включаю лимит устройств"
touch /var/log/3xipl.log /var/log/3xipl-banned.log
cat > /etc/fail2ban/filter.d/3x-ipl.conf <<'F2B'
[Definition]
datepattern = ^%%Y/%%m/%%d %%H:%%M:%%S
failregex   = \[LIMIT_IP\]\s*Email\s*=\s*<F-USER>.+</F-USER>\s*\|\|\s*SRC\s*=\s*<ADDR>
ignoreregex =
F2B
printf '[3x-ipl]\nenabled = true\nfilter = 3x-ipl\nlogpath = /var/log/3xipl.log\nmaxretry = 3\nfindtime = 100\nbantime = 15m\n' > /etc/fail2ban/jail.d/3x-ipl.conf
systemctl enable fail2ban >/dev/null 2>&1
systemctl restart fail2ban

echo "== Ставлю бота"
mkdir -p "$APP"; cd "$APP"
curl -fsSL "$RAW" -o vpn_bot.py
python3 -m venv venv
./venv/bin/pip install -q --upgrade pip
./venv/bin/pip install -q "aiogram>=3.13,<4" aiohttp
cat > .env <<ENV
BOT_TOKEN=$BOT_TOKEN
ADMIN_IDS=$ADMIN_ID
PANEL_URL=$URL
PANEL_USER=$PUSER
PANEL_PASS=$PPASS
INBOUND_ID=$INB
SERVER_HOST=$IP
PAY_INFO="$PAY_INFO"
ENV
chmod 600 .env
cat > /etc/systemd/system/vpnbot.service <<UNIT
[Unit]
Description=VPN seller bot
After=network-online.target x-ui.service
[Service]
WorkingDirectory=$APP
EnvironmentFile=$APP/.env
ExecStart=$APP/venv/bin/python vpn_bot.py
Restart=always
RestartSec=5
[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable --now vpnbot
sleep 6
systemctl is-active --quiet vpnbot && echo "БОТ РАБОТАЕТ" || echo "Бот не запустился: journalctl -u vpnbot -n 30"
echo; cat /root/panel_info.txt
