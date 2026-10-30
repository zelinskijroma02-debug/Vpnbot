#!/usr/bin/env bash
# Установка VPN-бота на сервер (Ubuntu/Debian) одной командой.
set -euo pipefail

[ "$(id -u)" -eq 0 ] || { echo "Запусти от root (команда: sudo -i)"; exit 1; }
APP=/opt/vpnbot

echo "== Установка пакетов =="
apt-get update -y
apt-get install -y python3 python3-venv python3-pip git curl
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)' \
  || { echo "Нужен Python 3.10+. Обнови систему (Ubuntu 22.04+)."; exit 1; }

if [ -d "$APP/.git" ]; then
  echo "== Обновляю код =="
  git -C "$APP" pull
else
  read -rp "Ссылка на GitHub-репозиторий (https://github.com/логин/репо): " REPO
  git clone "$REPO" "$APP"
fi
cd "$APP"
[ -f vpn_bot.py ] || { echo "В репозитории нет vpn_bot.py"; exit 1; }

echo "== Python-окружение =="
python3 -m venv venv
venv/bin/pip install -q --upgrade pip
venv/bin/pip install -q -r requirements.txt

if [ -f "$APP/.env" ]; then
  echo "== Файл настроек .env уже есть, оставляю его =="
else
  echo "== Настройки (значения из Render -> Environment) =="
  read -rsp "BOT_TOKEN (ввод не виден): " BOT_TOKEN; echo
  read -rp  "ADMIN_IDS (ваш Telegram ID, можно несколько через запятую): " ADMIN_IDS
  read -rp  "PANEL_URL [http://127.0.0.1:45515/y8hvbthznwp0/]: " PANEL_URL
  PANEL_URL=${PANEL_URL:-http://127.0.0.1:45515/y8hvbthznwp0/}
  read -rsp "PANEL_TOKEN (API-токен панели, ввод не виден): " PANEL_TOKEN; echo
  read -rp  "INBOUND_ID (число): " INBOUND_ID
  read -rp  "SERVER_HOST [109.248.163.230]: " SERVER_HOST
  SERVER_HOST=${SERVER_HOST:-109.248.163.230}
  read -rp  "PAY_INFO (реквизиты для оплаты, одной строкой): " PAY_INFO
  read -rp  "SUPPORT_USERNAME (ник поддержки без @, можно пусто): " SUPPORT_USERNAME
  umask 077
  cat > "$APP/.env" <<ENVEOF
BOT_TOKEN=$BOT_TOKEN
ADMIN_IDS=$ADMIN_IDS
PANEL_URL=$PANEL_URL
PANEL_TOKEN=$PANEL_TOKEN
INBOUND_ID=$INBOUND_ID
SERVER_HOST=$SERVER_HOST
PAY_INFO=$PAY_INFO
SUPPORT_USERNAME=$SUPPORT_USERNAME
DB_PATH=$APP/bot.db
ENVEOF
fi
chmod 600 "$APP/.env"

echo "== Автозапуск (systemd) =="
cat > /etc/systemd/system/vpnbot.service <<UNIT
[Unit]
Description=VPN bot
After=network-online.target
Wants=network-online.target

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
systemctl enable vpnbot >/dev/null
systemctl restart vpnbot
sleep 5

echo
if systemctl is-active --quiet vpnbot; then
  echo "✅ Бот запущен и будет работать 24/7, в том числе после перезагрузки сервера."
else
  echo "❌ Бот не запустился. Последние строки лога:"
fi
journalctl -u vpnbot -n 15 --no-pager || true
echo
echo "Логи:        journalctl -u vpnbot -f"
echo "Обновление:  cd $APP && git pull && systemctl restart vpnbot"
echo "Важно: остановите бота на Render (Settings -> Suspend Service)."
