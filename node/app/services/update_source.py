"""Откуда апдейтер ноды берёт код обновления: GitHub, при его недоступности — зеркало.

Склонированный код исполняется на хосте от root (apply-update.sh через nsenter),
а зеркало — чужой сервер. Поэтому оно используется, только когда панель заранее
узнала у самого GitHub, на какой коммит указывает ветка (expected_commit), и клон
с зеркала с ним совпал. Без expected_commit (запрос старой панели) зеркала нет —
только github.com. Та же схема у апдейтера панели (panel/backend/app/services/update_source.py).
"""

REPO_URL = "https://github.com/Joliz1337/monitoring.git"
MIRROR_URL = f"https://ghfast.top/{REPO_URL}"
COMMIT_SHA_PATTERN = r"^[0-9a-f]{40}$"

GITHUB_CLONE_TIMEOUT = 30
PROXY_CLONE_TIMEOUT = 60
MIRROR_CLONE_TIMEOUT = 120


def _clone(ref: str, url: str, clone_dir: str) -> str:
    return f'clone --depth 1 --branch "{ref}" "{url}" "{clone_dir}" 2>&1'


def _mirror_fallback(ref: str, clone_dir: str, expected_commit: str | None) -> str:
    if not expected_commit:
        return (
            '    echo "[ERROR] GitHub is unreachable; the mirror is not used: '
            'the panel did not pass a commit to verify it against"'
        )
    return f"""    rm -rf "{clone_dir}"
    echo "[WARN] GitHub timeout/error, trying mirror (ghfast.top)..."
    if timeout {MIRROR_CLONE_TIMEOUT} git {_clone(ref, MIRROR_URL, clone_dir)}; then
        CLONED_COMMIT=$(git -C "{clone_dir}" rev-parse HEAD 2>/dev/null || true)
        if [ "$CLONED_COMMIT" = "{expected_commit}" ]; then
            echo "[INFO] Mirror commit verified: {expected_commit}"
            CLONE_SUCCESS=1
        else
            echo "[ERROR] Mirror returned commit ${{CLONED_COMMIT:-unknown}} instead of {expected_commit}, update aborted"
            rm -rf "{clone_dir}"
        fi
    fi"""


def render_clone_step(
    ref: str,
    clone_dir: str,
    expected_commit: str | None,
    proxy: str | None = None,
) -> str:
    """sh-фрагмент апдейтера: клон ref в clone_dir, при неудаче — exit 1 до запуска кода.

    ref, expected_commit и proxy уже прошли паттерны UpdateRequest — ни пробелов,
    ни метасимволов shell в них нет. Через прокси оператора зеркало не пробуется:
    прокси и есть способ достучаться до github.com.
    """
    if proxy:
        attempt = f"""echo "[INFO] Using proxy for git: {proxy}"
echo "[INFO] Trying GitHub via proxy ({PROXY_CLONE_TIMEOUT}s timeout)..."
if timeout {PROXY_CLONE_TIMEOUT} git -c http.proxy={proxy} -c https.proxy={proxy} {_clone(ref, REPO_URL, clone_dir)}; then
    CLONE_SUCCESS=1
fi"""
    else:
        attempt = f"""echo "[INFO] Trying GitHub ({GITHUB_CLONE_TIMEOUT}s timeout)..."
if timeout {GITHUB_CLONE_TIMEOUT} git {_clone(ref, REPO_URL, clone_dir)}; then
    CLONE_SUCCESS=1
else
{_mirror_fallback(ref, clone_dir, expected_commit)}
fi"""

    return f"""rm -rf "{clone_dir}"
CLONE_SUCCESS=0
{attempt}

if [ $CLONE_SUCCESS -eq 0 ]; then
    echo "[ERROR] Failed to clone repository"
    exit 1
fi"""
