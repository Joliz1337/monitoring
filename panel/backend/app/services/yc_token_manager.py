import logging
import time

import httpx

logger = logging.getLogger(__name__)

YC_IAM_ENDPOINT = "https://iam.api.cloud.yandex.net/iam/v1/tokens"
TOKEN_TTL = 3500  # кэш ~58 минут
EXCHANGE_TIMEOUT = 10.0


class YCTokenError(Exception):
    pass


class YCTokenManager:
    """OAuth-токен → IAM-токен с кэшированием."""

    def __init__(self):
        self._cache: dict[str, tuple[str, float]] = {}

    async def get_iam_token(self, client: httpx.AsyncClient, oauth_token: str) -> str:
        """Обмен идёт через клиент вызывающего — с тем же прокси, что и запросы биллинга."""
        cache_key = oauth_token[:16]
        cached = self._cache.get(cache_key)
        if cached and time.time() < cached[1]:
            return cached[0]

        resp = await client.post(
            YC_IAM_ENDPOINT,
            json={"yandexPassportOauthToken": oauth_token},
            timeout=EXCHANGE_TIMEOUT,
        )

        if resp.status_code != 200:
            logger.error("YC IAM token exchange failed: %s %s", resp.status_code, resp.text[:300])
            raise YCTokenError(f"IAM token exchange failed: HTTP {resp.status_code}")

        iam_token = resp.json()["iamToken"]
        self._cache[cache_key] = (iam_token, time.time() + TOKEN_TTL)
        return iam_token


_instance: YCTokenManager | None = None


def get_yc_token_manager() -> YCTokenManager:
    global _instance
    if _instance is None:
        _instance = YCTokenManager()
    return _instance
