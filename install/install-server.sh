#!/usr/bin/env bash
# Установка серверной части «Дачи» на Ubuntu/Debian одной командой.
#
#   curl -fsSL https://raw.githubusercontent.com/coldout456-cloud/ters1/main/install/install-server.sh | sudo bash -s -- ИМЯ.duckdns.org
#
# Скрипт можно запускать повторно: он обновляет код и службу, но не трогает
# готовый .env (пароль, токены) и ничего не делает с другими службами сервера.
set -euo pipefail

REPO="${REPO:-coldout456-cloud/ters1}"
BRANCH="${BRANCH:-main}"
APP_USER="dacha"
BASE="/opt/dacha"
APP="$BASE/app"
SRV="$APP/server"

say()  { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
ok()   { printf '    \033[32m%s\033[0m\n' "$*"; }
warn() { printf '    \033[33m! %s\033[0m\n' "$*"; }
die()  { printf '\n\033[1;31mОШИБКА: %s\033[0m\n' "$*" >&2; exit 1; }

# Команда от имени служебного пользователя (HOME задаём явно, чтобы git и pip не лезли в /root)
as_app() { runuser -u "$APP_USER" -- env HOME="$BASE" "$@"; }

# Записать KEY=VALUE в .env (заменить, если уже есть)
set_env() {
  local key="$1" val="$2" f="$SRV/.env"
  if grep -q "^${key}=" "$f"; then
    sed -i "s|^${key}=.*|${key}=${val}|" "$f"
  else
    [ -z "$(tail -c1 "$f")" ] || echo >>"$f"
    echo "${key}=${val}" >>"$f"
  fi
}

main() {
  local DOMAIN="${1:-}"

  [ "$(id -u)" -eq 0 ] || die "Запусти от root (или через sudo)."
  command -v apt-get >/dev/null || die "Скрипт рассчитан на Ubuntu/Debian."
  [ -n "$DOMAIN" ] || die "Не указан домен. Пример: ... | sudo bash -s -- coldout-dacha.duckdns.org"
  [[ "$DOMAIN" =~ ^[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?\.[A-Za-z]{2,}$ ]] || die "Домен выглядит неправильно: $DOMAIN"

  # ---------------------------------------------------------------- 1. проверки
  say "Проверяю домен и порты"
  local PUBLIC_IP DNS_IP
  PUBLIC_IP="$(curl -4 -fsS --max-time 10 https://ifconfig.me 2>/dev/null || curl -4 -fsS --max-time 10 https://api.ipify.org 2>/dev/null || true)"
  DNS_IP="$(getent ahostsv4 "$DOMAIN" 2>/dev/null | awk 'NR==1{print $1}' || true)"
  [ -n "$DNS_IP" ] || die "Домен $DOMAIN пока не указывает ни на какой IP. Проверь запись на DuckDNS и повтори через минуту."
  if [ -n "$PUBLIC_IP" ] && [ "$DNS_IP" != "$PUBLIC_IP" ]; then
    if [ "${SKIP_DNS_CHECK:-0}" = "1" ]; then
      warn "Домен указывает на $DNS_IP, а IP сервера — $PUBLIC_IP. Продолжаю, потому что SKIP_DNS_CHECK=1."
    else
      die "Домен указывает на $DNS_IP, а IP этого сервера — $PUBLIC_IP. Впиши $PUBLIC_IP в DuckDNS (update ip), подожди минуту и повтори."
    fi
  fi
  ok "Домен $DOMAIN -> $DNS_IP"

  local p owner
  for p in 80 443; do
    owner="$(ss -H -tlnp "sport = :$p" 2>/dev/null | head -1 || true)"
    if [ -n "$owner" ] && ! grep -q caddy <<<"$owner"; then
      die "Порт $p уже занят другой программой: $owner
Ничего не менял. Пришли мне этот вывод — подключим сайт к уже работающему веб-серверу."
    fi
  done
  ok "Порты 80 и 443 свободны"

  # ---------------------------------------------------------------- 2. пакеты
  say "Ставлю нужные пакеты (без обновления остальной системы)"
  export DEBIAN_FRONTEND=noninteractive
  export NEEDRESTART_MODE=l   # только список: ничего не перезапускать (на сервере живут боты)
  apt-get update -y -qq
  apt-get install -y -qq python3 python3-venv python3-pip git curl ca-certificates gnupg \
    debian-keyring debian-archive-keyring apt-transport-https

  # ---------------------------------------------------------------- 3. код
  say "Скачиваю код с GitHub ($REPO)"
  if ! id "$APP_USER" >/dev/null 2>&1; then
    adduser --system --group --home "$BASE" "$APP_USER" >/dev/null
  fi
  mkdir -p "$BASE"
  chown "$APP_USER:$APP_USER" "$BASE"

  if [ -d "$APP/.git" ]; then
    as_app git -C "$APP" pull --ff-only
  elif [ -d "$APP" ] && [ -n "$(ls -A "$APP" 2>/dev/null)" ]; then
    die "$APP уже существует и это не клон репозитория. Переименуй или удали эту папку и запусти ещё раз."
  else
    rm -rf "$APP"
    as_app git clone --branch "$BRANCH" "https://github.com/$REPO.git" "$APP"
  fi
  [ -f "$SRV/server.py" ] || die "В репозитории нет server/server.py. Проверь, что файлы загружены на GitHub."
  ok "Код на месте: $APP"

  # ---------------------------------------------------------------- 4. окружение
  say "Готовлю Python-окружение"
  [ -x "$SRV/.venv/bin/python" ] || as_app python3 -m venv "$SRV/.venv"
  as_app "$SRV/.venv/bin/pip" install -q --disable-pip-version-check -r "$SRV/requirements.txt"
  ok "Библиотеки установлены"

  # ---------------------------------------------------------------- 5. настройки
  if [ -f "$SRV/.env" ]; then
    say "Настройки (.env) уже есть — оставляю как есть"
  else
    say "Создаю настройки (.env)"
    ( : </dev/tty ) 2>/dev/null || die "Нужен интерактивный сеанс SSH (скрипт спросит пароль для входа на сайт)."
    ( cd "$SRV" && python3 - <<'PYEOF'
import getpass, os, secrets, shutil
from security import hash_password

pw = getpass.getpass("Придумай пароль для входа на сайт (не короче 12 символов): ")
if len(pw) < 12:
    raise SystemExit("Пароль слишком короткий, запусти скрипт ещё раз.")
if getpass.getpass("Повтори пароль: ") != pw:
    raise SystemExit("Пароли не совпали, запусти скрипт ещё раз.")

lines = [
    "AGENT_URL=http://100.64.0.1:8765",
    f"AGENT_TOKEN={secrets.token_urlsafe(32)}",
    "ADMIN_USER=admin",
    f"ADMIN_PASSWORD_HASH={hash_password(pw)}",
    f"SESSION_SECRET={secrets.token_urlsafe(48)}",
    "SESSION_HOURS=12",
    "COOKIE_SECURE=1",
    "TRUST_PROXY=1",
    "SERVER_HOST=127.0.0.1",
    "SERVER_PORT=8000",
]
with open(".env", "w") as f:
    f.write("\n".join(lines) + "\n")
os.chmod(".env", 0o600)
shutil.chown(".env", "dacha", "dacha")
PYEOF
    )
    ok "Файл .env создан (логин: admin)"
  fi
  # Адрес сайта нужен, чтобы сайт мог выдавать установщик для дачи
  set_env PUBLIC_URL "https://$DOMAIN"
  chown "$APP_USER:$APP_USER" "$SRV/.env"
  chmod 600 "$SRV/.env"

  local PORT TOKEN
  PORT="$(grep -E '^SERVER_PORT=' "$SRV/.env" | head -1 | cut -d= -f2 | tr -d '\r' || true)"
  PORT="${PORT:-8000}"
  TOKEN="$(grep -E '^AGENT_TOKEN=' "$SRV/.env" | head -1 | cut -d= -f2 | tr -d '\r' || true)"

  # ---------------------------------------------------------------- 6. служба
  say "Настраиваю автозапуск сервера (systemd)"
  cat >/etc/systemd/system/dacha.service <<UNIT
[Unit]
Description=Dacha remote server
After=network-online.target
Wants=network-online.target

[Service]
User=$APP_USER
Group=$APP_USER
WorkingDirectory=$SRV
ExecStart=$SRV/.venv/bin/python server.py
Restart=always
RestartSec=3
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
UNIT
  systemctl daemon-reload
  systemctl enable dacha >/dev/null 2>&1
  systemctl restart dacha

  local code="" i
  for i in $(seq 1 20); do
    code="$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT/login" || true)"
    [ "$code" = "200" ] && break
    sleep 1
  done
  if [ "$code" != "200" ]; then
    journalctl -u dacha -n 30 --no-pager || true
    die "Сервер не отвечает на 127.0.0.1:$PORT (последние строки лога выше)."
  fi
  ok "Сервер работает на 127.0.0.1:$PORT"

  # Помощник: после установки агента на даче одной командой указать его адрес
  cat >/usr/local/bin/dacha-set-agent <<'HELPER'
#!/usr/bin/env bash
# Использование: dacha-set-agent 100.x.y.z   (адрес дачи в Tailscale)
set -euo pipefail
ip="${1:-}"
[[ "$ip" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]] || { echo "Использование: dacha-set-agent 100.x.y.z"; exit 1; }
f=/opt/dacha/app/server/.env
sed -i "s|^AGENT_URL=.*|AGENT_URL=http://$ip:8765|" "$f"
rm -f /opt/dacha/app/server/agent_state.json   # забыть адрес, который агент сообщал сам
chown dacha:dacha "$f"
chmod 600 "$f"
systemctl restart dacha
echo "Готово: сервер теперь обращается к агенту по адресу $ip"
HELPER
  chmod +x /usr/local/bin/dacha-set-agent

  # Помощник: сохранить ключ Tailscale, чтобы установщик для дачи входил в VPN сам
  cat >/usr/local/bin/dacha-set-tskey <<'HELPER2'
#!/usr/bin/env bash
# Использование: dacha-set-tskey [tskey-auth-...]   (без аргумента спросит ключ скрытым вводом)
set -euo pipefail
key="${1:-}"
if [ -z "$key" ]; then
  read -rsp "Вставь ключ Tailscale (tskey-auth-...): " key </dev/tty
  echo
fi
[[ "$key" =~ ^tskey-[A-Za-z0-9-]{10,200}$ ]] || { echo "Это не похоже на ключ Tailscale (должен начинаться с tskey-)."; exit 1; }
f=/opt/dacha/app/server/.env
if grep -q '^TAILSCALE_AUTHKEY=' "$f"; then
  sed -i "s|^TAILSCALE_AUTHKEY=.*|TAILSCALE_AUTHKEY=$key|" "$f"
else
  [ -z "$(tail -c1 "$f")" ] || echo >>"$f"
  echo "TAILSCALE_AUTHKEY=$key" >>"$f"
fi
chown dacha:dacha "$f"
chmod 600 "$f"
systemctl restart dacha
echo "Готово: ключ сохранён, установщик для дачи теперь вшивает его."
HELPER2
  chmod +x /usr/local/bin/dacha-set-tskey

  # ---------------------------------------------------------------- 7. Caddy (HTTPS)
  say "Настраиваю HTTPS (Caddy)"
  if ! command -v caddy >/dev/null 2>&1; then
    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --dearmor --yes -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' >/etc/apt/sources.list.d/caddy-stable.list
    chmod o+r /usr/share/keyrings/caddy-stable-archive-keyring.gpg /etc/apt/sources.list.d/caddy-stable.list
    apt-get update -y -qq
    apt-get install -y -qq caddy
  fi

  local CADDYFILE=/etc/caddy/Caddyfile
  mkdir -p /etc/caddy/conf.d
  cat >/etc/caddy/conf.d/dacha.caddy <<CADDY
$DOMAIN {
    reverse_proxy 127.0.0.1:$PORT {
        flush_interval -1
    }
}
CADDY
  if [ ! -f "$CADDYFILE" ] || grep -q '/usr/share/caddy' "$CADDYFILE"; then
    [ -f "$CADDYFILE" ] && cp "$CADDYFILE" "$CADDYFILE.bak"
    echo 'import /etc/caddy/conf.d/*.caddy' >"$CADDYFILE"
  elif ! grep -q 'conf.d/\*.caddy' "$CADDYFILE"; then
    cp "$CADDYFILE" "$CADDYFILE.bak"
    printf '\nimport /etc/caddy/conf.d/*.caddy\n' >>"$CADDYFILE"
  fi
  caddy validate --config "$CADDYFILE" --adapter caddyfile >/dev/null 2>&1 \
    || die "Caddy не принял конфигурацию. Проверь: caddy validate --config $CADDYFILE"
  systemctl enable caddy >/dev/null 2>&1
  systemctl reload-or-restart caddy

  say "Жду сертификат HTTPS (до минуты)"
  local https_ok=0
  for i in $(seq 1 12); do
    if curl -fsS -o /dev/null --max-time 10 "https://$DOMAIN/login" 2>/dev/null; then https_ok=1; break; fi
    sleep 5
  done
  if [ "$https_ok" = "1" ]; then
    ok "https://$DOMAIN отвечает"
  else
    warn "Пока не удалось открыть https://$DOMAIN. Сертификат мог ещё не выпуститься."
    warn "Если так и не заработает: journalctl -u caddy -n 30 --no-pager"
    warn "Частая причина — порты 80 и 443 закрыты в панели хостинга (файрвол провайдера)."
  fi

  # ---------------------------------------------------------------- 8. файрвол
  if command -v ufw >/dev/null 2>&1 && ufw status | grep -q '^Status: active'; then
    ufw allow 80/tcp >/dev/null
    ufw allow 443/tcp >/dev/null
    ok "ufw включён: порты 80 и 443 открыты"
  else
    ok "ufw выключен — правила не трогаю (чтобы не задеть твоих ботов)"
  fi

  # ---------------------------------------------------------------- 9. Tailscale
  say "Настраиваю VPN (Tailscale)"
  if ! command -v tailscale >/dev/null 2>&1; then
    curl -fsSL https://tailscale.com/install.sh | sh
  fi
  if ! tailscale status >/dev/null 2>&1; then
    echo "    Сейчас появится ссылка. Открой её в браузере и войди в аккаунт Tailscale."
    echo "    (на даче потом нужно войти в ТОТ ЖЕ аккаунт)"
    tailscale up
  fi
  ok "Tailscale-адрес сервера: $(tailscale ip -4 2>/dev/null | head -1)"

  # ---------------------------------------------------------------- итог
  printf '\n\033[1;32m================ ГОТОВО ================\033[0m\n'
  echo "Сайт:   https://$DOMAIN   (логин: admin, пароль — тот, что ты придумал)"
  echo
  echo "Установка на дачу — в один клик, ничего вводить не нужно:"
  echo "  1. На дачном компьютере открой https://$DOMAIN и войди."
  echo "  2. Вкладка «Установка» -> «Скачать установщик для дачи»."
  echo "  3. Запусти скачанный файл двойным кликом и нажми «Да»."
  echo
  echo "Необязательно, но так на даче вообще не придётся входить в Tailscale вручную:"
  echo "  создай в панели Tailscale ключ (Settings -> Keys -> Generate auth key, галочка Reusable)"
  echo "  и выполни здесь команду:  dacha-set-tskey"
}

main "$@"
