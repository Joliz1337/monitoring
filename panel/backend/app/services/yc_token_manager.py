"""Учётные данные Yandex Cloud → IAM-токен с кэшем.

Основной вход — авторизованный ключ сервисного аккаунта (JSON-файл из консоли
или `yc iam key create`): им подписывается короткоживущий JWT (PS256), который
IAM меняет на IAM-токен. Новые OAuth-токены Яндекс ID Yandex Cloud не принимает
с 1 июня 2026 года, а выданные раньше действуют до конца срока — такие ещё
меняются напрямую, чтобы старые проекты не сломались в день обновления панели.
"""
import hashlib
import json
import logging
import time
from dataclasses import dataclass

import httpx
import jwt

logger = logging.getLogger(__name__)

YC_IAM_ENDPOINT = "https://iam.api.cloud.yandex.net/iam/v1/tokens"
TOKEN_TTL = 3500  # кэш ~58 минут
EXCHANGE_TIMEOUT = 10.0
# IAM принимает JWT, живущий не дольше часа: exp - iat ≤ 3600
JWT_LIFETIME = 3600
PRIVATE_KEY_MARKER = "-----BEGIN PRIVATE KEY-----"
OAUTH_DEPRECATION_HINT = (
    "Yandex Cloud no longer accepts new OAuth tokens, use a service account authorized key"
)


class YCTokenError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class AuthorizedKey:
    key_id: str
    service_account_id: str
    private_key: str


def parse_authorized_key(credential: str) -> AuthorizedKey | None:
    """Авторизованный ключ из JSON-файла; None — в поле OAuth-токен."""
    text = credential.strip()
    if not text.startswith("{"):
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise YCTokenError("Authorized key is not valid JSON") from e

    fields = ("id", "service_account_id", "private_key")
    values = [data.get(name) if isinstance(data, dict) else None for name in fields]
    if not all(isinstance(value, str) and value for value in values):
        raise YCTokenError("Authorized key must contain id, service_account_id and private_key")

    key_id, service_account_id, private_key = values
    # Перед PEM у ключей Yandex бывает служебная строка «PLEASE DO NOT REMOVE THIS LINE!»,
    # поэтому маркер ищется внутри значения, а не в начале
    if PRIVATE_KEY_MARKER not in private_key:
        raise YCTokenError("Authorized key has no private key in PEM format")
    return AuthorizedKey(key_id, service_account_id, private_key)


def _signed_jwt(key: AuthorizedKey, now: float) -> str:
    issued_at = int(now)
    payload = {
        "iss": key.service_account_id,
        "aud": YC_IAM_ENDPOINT,
        "iat": issued_at,
        "exp": issued_at + JWT_LIFETIME,
    }
    try:
        return jwt.encode(payload, key.private_key, algorithm="PS256", headers={"kid": key.key_id})
    except (ValueError, TypeError, jwt.PyJWTError) as e:
        raise YCTokenError(f"Cannot sign with the authorized key: {e}") from e


def _error_detail(resp: httpx.Response) -> str:
    try:
        body = resp.json()
    except ValueError:
        return ""
    return str(body.get("message") or "") if isinstance(body, dict) else ""


class YCTokenManager:
    """Авторизованный ключ сервисного аккаунта или OAuth-токен → IAM-токен с кэшем."""

    def __init__(self):
        self._cache: dict[str, tuple[str, float]] = {}

    async def get_iam_token(self, client: httpx.AsyncClient, credential: str) -> str:
        """Обмен идёт через клиент вызывающего — с тем же прокси, что и запросы биллинга."""
        # Хэш всего значения: у JSON-ключей одинаковое начало, у OAuth-токенов
        # одного пользователя — тоже, и префикс выдал бы чужой IAM-токен
        cache_key = hashlib.sha256(credential.encode("utf-8")).hexdigest()
        now = time.time()
        cached = self._cache.get(cache_key)
        if cached and now < cached[1]:
            return cached[0]

        key = parse_authorized_key(credential)
        body = {"jwt": _signed_jwt(key, now)} if key else {"yandexPassportOauthToken": credential.strip()}
        resp = await client.post(YC_IAM_ENDPOINT, json=body, timeout=EXCHANGE_TIMEOUT)

        if resp.status_code != 200:
            logger.error("YC IAM token exchange failed: %s %s", resp.status_code, resp.text[:300])
            message = f"IAM token exchange failed: HTTP {resp.status_code}"
            detail = _error_detail(resp)
            if detail:
                message += f": {detail}"
            if key is None and resp.status_code < 500:
                message += f" ({OAUTH_DEPRECATION_HINT})"
            raise YCTokenError(message)

        iam_token = resp.json()["iamToken"]
        self._cache[cache_key] = (iam_token, now + TOKEN_TTL)
        return iam_token


_instance: YCTokenManager | None = None


def get_yc_token_manager() -> YCTokenManager:
    global _instance
    if _instance is None:
        _instance = YCTokenManager()
    return _instance
