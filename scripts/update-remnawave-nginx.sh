#!/bin/sh
# Переводит nginx установки Remnawave (контейнер remnawave-nginx) на версию с исправлениями безопасности.
#
# Ветку, которую nginx больше не выпускает, меняет в docker-compose.yml на стабильную, скачивает образ
# и пересоздаёт контейнер. Конфиг проверяется на новом образе до пересоздания; nginx не поднялся —
# возвращается прежний образ. Выключенный в панели nginx не запускается. Повторный запуск ничего не трогает.
#
# На ноде:   sh update-remnawave-nginx.sh
# На парке:  панель → «Массовые действия» → терминал, таймаут 300 с:
#   curl -fsSL https://raw.githubusercontent.com/Joliz1337/monitoring/<ветка>/scripts/update-remnawave-nginx.sh | sh
#
# Следующий выпуск безопасности nginx — поднять FIXED_*. Смена стабильной ветки — ещё и STABLE_BRANCH вместе
# с образом в install.sh и FALLBACK_NGINX_IMAGE ноды: их сверяет node/tests/test_remnawave_nginx_image.py.

STABLE_BRANCH=1.30
FIXED_STABLE=1.30.7
FIXED_MAINLINE=1.31.9

INSTALL_DIR="${REMNAWAVE_DIR:-/opt/remnawave}"
CONTAINER=remnawave-nginx
COMPOSE="$INSTALL_DIR/docker-compose.yml"
BACKUP="$COMPOSE.bak-nginx"
START_GRACE_SEC=5
EXIT_STILL_VULNERABLE=2

lowest_of() { printf '%s\n%s\n' "$1" "$2" | sort -V | head -n1; }
older() { [ "$1" != "$2" ] && [ "$(lowest_of "$1" "$2")" = "$1" ]; }
fixed() {
    case "$1" in "$STABLE_BRANCH".*) lowest_fixed=$FIXED_STABLE ;; *) lowest_fixed=$FIXED_MAINLINE ;; esac
    [ -n "$1" ] && [ "$(lowest_of "$lowest_fixed" "$1")" = "$lowest_fixed" ]
}
running_version() { docker exec "$CONTAINER" nginx -v 2>&1 | sed -n 's|.*nginx/\([0-9.]*\).*|\1|p'; }
container_field() { docker inspect -f "$1" "$CONTAINER" 2>/dev/null; }
restore_compose() { cp -p "$BACKUP" "$COMPOSE"; }

# Тег образа → тег, под которым выходит исправление; пусто — менять нечего.
# Теги без номера (alpine, stable, mainline) обновляет само скачивание.
target_tag() {
    version=${1%%-*}
    flavor=${1#"$version"}
    case "$version" in [0-9]*.[0-9]*) ;; *) return ;; esac
    branch=$(echo "$version" | cut -d. -f1,2)
    if older "$branch" "$STABLE_BRANCH"; then
        echo "$STABLE_BRANCH$flavor"
    elif [ "$version" != "$branch" ] && ! fixed "$version"; then
        echo "$branch$flavor"
    fi
}

retarget_images() {
    sed -n -E 's#^[[:space:]]*image:[[:space:]]*nginx:([A-Za-z0-9.-]+)[[:space:]]*$#\1#p' "$COMPOSE" | sort -u |
    while read -r tag; do
        new_tag=$(target_tag "$tag")
        [ -n "$new_tag" ] || continue
        tag_pattern=$(printf '%s' "$tag" | sed 's/\./\\./g')
        sed -i -E "s#^([[:space:]]*image:[[:space:]]*nginx:)$tag_pattern([[:space:]]*)\$#\\1$new_tag\\2#" "$COMPOSE"
    done
}

if [ ! -f "$COMPOSE" ] || ! docker inspect "$CONTAINER" >/dev/null 2>&1; then
    echo "SKIP: нет $CONTAINER"
    exit 0
fi
cd "$INSTALL_DIR" || exit 1

state=$(container_field '{{.State.Status}}')
before=$(running_version)
if [ "$state" = running ] && fixed "$before"; then
    echo "OK: уже $before"
    exit 0
fi

cp -p "$COMPOSE" "$BACKUP"
retarget_images
if ! pull_error=$(docker compose pull -q "$CONTAINER" 2>&1 >/dev/null); then
    restore_compose
    echo "FAIL: образ не скачался, ничего не изменено: $(echo "$pull_error" | tail -n1)"
    exit 1
fi
if [ "$state" != running ]; then
    echo "OK: $CONTAINER выключен — новый образ скачан, запускать не стал"
    exit 0
fi

# Проверка на новом образе до пересоздания: при отказе работающий nginx остаётся нетронутым
if ! test_output=$(docker compose run --rm --no-deps -T --entrypoint nginx "$CONTAINER" -t 2>&1); then
    restore_compose
    echo "FAIL: конфиг не проходит проверку на новом nginx, ничего не изменено:"
    echo "$test_output" | tail -n5
    exit 1
fi

previous_id=$(container_field '{{.Id}}')
docker compose up -d --no-deps "$CONTAINER" >/dev/null 2>&1
# Счётчик рестартов обнуляет только пересоздание: у прежнего контейнера он остался бы от старых падений
if [ "$(container_field '{{.Id}}')" != "$previous_id" ]; then
    sleep "$START_GRACE_SEC"
    if [ "$(container_field '{{.State.Status}}/{{.RestartCount}}')" != running/0 ]; then
        echo "FAIL: nginx не поднялся на новом образе, возвращаю прежний:"
        docker logs --tail 10 "$CONTAINER" 2>&1
        restore_compose
        docker compose up -d --no-deps "$CONTAINER" >/dev/null 2>&1
        sleep "$START_GRACE_SEC"
        echo "после отката: $(container_field '{{.State.Status}}') $(running_version)"
        exit 1
    fi
fi

after=$(running_version)
if fixed "$after"; then
    echo "OK: ${before:-?} -> $after"
    exit 0
fi
echo "WARN: ${before:-?} -> ${after:-?}, версия осталась уязвимой — образ задан вручную: $(container_field '{{.Config.Image}}')"
exit "$EXIT_STILL_VULNERABLE"
