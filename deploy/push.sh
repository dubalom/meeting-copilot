#!/usr/bin/env bash
# Upload the project to the server and (re)start it.  Usage (from the project folder on your Mac):
#   ./deploy/push.sh root@SERVER_IP              # code + .env + context/ (not your private data/)
#   ./deploy/push.sh root@SERVER_IP --with-data  # also upload data/ (your saved chats & profile)
set -euo pipefail
TARGET="${1:?usage: ./deploy/push.sh root@SERVER_IP [--with-data]}"
WITH_DATA="${2:-}"
DIR=/opt/copilot
cd "$(dirname "$0")/.."

grep -q '^DOMAIN=.\+' .env || { echo "Впиши DOMAIN=твой-домен.ai в .env"; exit 1; }
grep -q '^APP_TOKEN=.\+' .env || { echo "Впиши APP_TOKEN в .env"; exit 1; }

EXCLUDES=(--exclude .venv --exclude __pycache__ --exclude .pytest_cache --exclude sessions --exclude .DS_Store
          --exclude desktop/node_modules --exclude desktop/dist --exclude desktop/build --exclude downloads)
[ "$WITH_DATA" = "--with-data" ] || EXCLUDES+=(--exclude data)

echo "==> upload to $TARGET:$DIR"
rsync -az --delete "${EXCLUDES[@]}" ./ "$TARGET:$DIR/"
# desktop installers for the public /download page (built with: cd desktop && npm run dist:mac / dist:win)
shopt -s nullglob
BUILDS=(desktop/dist/*.dmg desktop/dist/*-Setup-*.exe desktop/dist/*-win.zip)
if [ ${#BUILDS[@]} -gt 0 ]; then
  echo "==> installers: ${BUILDS[*]##*/}"
  ssh "$TARGET" "mkdir -p $DIR/downloads"
  rsync -a --delete --progress "${BUILDS[@]}" "$TARGET:$DIR/downloads/"
fi
ssh "$TARGET" "mkdir -p $DIR/data $DIR/downloads && chown -R 10001:10001 $DIR && chmod 600 $DIR/.env && cd $DIR && docker compose up -d --build && docker compose ps"

DOMAIN=$(grep '^DOMAIN=' .env | cut -d= -f2-)
TOKEN=$(grep '^APP_TOKEN=' .env | cut -d= -f2-)
echo
echo "Готово. Первый запуск HTTPS-сертификата занимает ~30 с."
echo "Ссылка (не публикуй — в ней пароль): https://$DOMAIN/?token=$TOKEN"
