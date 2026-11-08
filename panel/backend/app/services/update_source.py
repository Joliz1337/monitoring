"""Откуда апдейтер панели берёт код обновления: GitHub, при его недоступности — зеркало.

Склонированный update.sh исполняется в privileged-контейнере с docker.sock, то есть
с root-доступом к хосту, а зеркало — чужой сервер. Поэтому оно используется, только
когда панель заранее узнала у самого GitHub, на какой коммит указывает ветка
(upstream_commit), и клон с зеркала с ним совпал. Не узнала — зеркала нет, только
github.com. Та же схема у апдейтера ноды (node/app/services/update_source.py).
"""

REPO_URL = "https://github.com/Joliz1337/monitoring.git"
MIRROR_URL = f"https://ghfast.top/{REPO_URL}"
COMMIT_SHA_PATTERN = r"^[0-9a-f]{40}$"

# Ссылка подставляется в текст shell-скрипта, который апдейтер исполняет в
# privileged-контейнере с docker.sock — то есть это прямой путь к произвольной
# команде на хосте панели. В наборе символов нет ни метасимвола shell, ни пробела;
# ведущий символ алфавитно-цифровой, иначе ссылка сошла бы у git за опцию вида
# --upload-pack=. Кавычки в самом скрипте — второй рубеж, не замена проверке.
GIT_REF_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._/-]*$"

GITHUB_CLONE_TIMEOUT = 30
MIRROR_CLONE_TIMEOUT = 120


def _clone(ref: str, url: str, clone_dir: str) -> str:
    return f'clone --depth 1 --branch "{ref}" "{url}" "{clone_dir}" 2>&1'


def _mirror_fallback(ref: str, clone_dir: str, expected_commit: str | None) -> str:
    if not expected_commit:
        return (
            '    echo "[ERROR] GitHub is unreachable; the mirror is not used: '
            'the commit to verify it against is unknown (GitHub API unreachable)"'
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


def render_clone_step(ref: str, clone_dir: str, expected_commit: str | None) -> str:
    """sh-фрагмент апдейтера: клон ref в clone_dir, при неудаче — exit 1 до запуска кода.

    ref прошёл GIT_REF_PATTERN, expected_commit — COMMIT_SHA_PATTERN: ни пробелов,
    ни метасимволов shell в них нет.
    """
    return f"""rm -rf "{clone_dir}"
CLONE_SUCCESS=0
echo "[INFO] Trying GitHub ({GITHUB_CLONE_TIMEOUT}s timeout)..."
if timeout {GITHUB_CLONE_TIMEOUT} git {_clone(ref, REPO_URL, clone_dir)}; then
    CLONE_SUCCESS=1
else
{_mirror_fallback(ref, clone_dir, expected_commit)}
fi

if [ $CLONE_SUCCESS -eq 0 ]; then
    echo "[ERROR] Failed to clone repository"
    exit 1
fi"""
