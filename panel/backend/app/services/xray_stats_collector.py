"""
Xray stats collector — fetches user IPs directly from Remnawave Panel API.

Collection flow (ephemeral — each cycle replaces all data):
1. GET /api/nodes → filter connected & enabled
2. POST /api/ip-control/fetch-users-ips/{nodeUuid} → jobId per node
3. Poll results → merge all (user_id, ip) pairs, filter ACTIVE users
4. DELETE all old IPs → INSERT fresh snapshot
"""

import asyncio
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import select, delete, func as sql_func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.database import async_session
from app.models import RemnawaveSettings, XrayStats, RemnawaveUserCache, RemnawaveHwidDevice, AlertSettings
from app.services.remnawave_api import get_remnawave_api, RemnawaveAPIError
from app.services.asn_lookup import lookup_ips_cached, group_ips_by_asn, effective_ip_count, enrich_with_names

logger = logging.getLogger(__name__)

UPSERT_BATCH_SIZE = 500
USER_CACHE_BATCH_SIZE = 100
HWID_BATCH_SIZE = 200
IP_POLL_CONCURRENCY = 3

KNOWN_UA_PATTERN = re.compile(
    r'^(v2raytun/(ios|android|windows)'
    r'|Clash-Meta/Prizrak-Box'
    r'|Happ/'
    r'|FlClash ?X/'
    r'|INCY/'
    r'|HiddifyNext/'
    r'|Hiddify/'
    r'|Flowvy/'
    r'|prizrak-box/'
    r'|koala-clash/'
    r')',
    re.IGNORECASE,
)

VERSION_PATTERN = re.compile(r'^[\d._]+$')


class XrayStatsCollector:

    def __init__(self):
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._user_cache_task: Optional[asyncio.Task] = None
        self._cleanup_task: Optional[asyncio.Task] = None

        self._collection_interval = 300
        self._user_cache_interval = 1800
        self._time_since_last_collect = 0

        self._last_collect_time: Optional[datetime] = None
        self._collecting = False
        self._last_nodes_count = 0

        self._last_user_cache_update: Optional[datetime] = None
        self._user_cache_updating = False

        self._ignored_user_ids: set[int] = set()
        self._db_write_lock = asyncio.Lock()

        # IP аномалии: email → (streak_count, last_ip_count)
        # Уведомление отправляется только после 5 подряд подтверждений
        self._ip_anomaly_streak: dict[int, tuple[int, int]] = {}

        # Traffic аномалии: снапшот used_traffic_bytes между циклами
        self._traffic_snapshot: dict[int, int] = {}
        self._traffic_anomaly_streak: dict[int, int] = {}
        self._traffic_snapshot_initialized: bool = False

    async def _get_ignored_user_ids(self) -> set[int]:
        async with async_session() as db:
            result = await db.execute(select(RemnawaveSettings).limit(1))
            settings = result.scalar_one_or_none()
            if not settings or not settings.ignored_user_ids:
                return set()
            try:
                data = json.loads(settings.ignored_user_ids)
                if isinstance(data, list):
                    return {int(x) for x in data if isinstance(x, (int, str)) and str(x).isdigit()}
                return set()
            except (json.JSONDecodeError, ValueError):
                return set()

    async def start(self):
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._collection_loop())
        self._user_cache_task = asyncio.create_task(self._user_cache_loop())
        self._cleanup_task = asyncio.create_task(self._cleanup_loop())

        from app.services.telegram_bot import get_telegram_bot_service
        bot_service = get_telegram_bot_service()
        bot_service.include_router(_rw_callback_router)

        logger.info("Xray stats collector started")

    async def stop(self):
        self._running = False
        for task in [self._task, self._user_cache_task, self._cleanup_task]:
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        logger.info("Xray stats collector stopped")

    async def _get_settings(self) -> Optional[RemnawaveSettings]:
        async with async_session() as db:
            result = await db.execute(select(RemnawaveSettings).limit(1))
            return result.scalar_one_or_none()

    # === Collection loop ===

    async def _collection_loop(self):
        self._time_since_last_collect = 0
        _SETTINGS_CHECK_INTERVAL = 15
        _settings_check_counter = 0
        _cached_enabled = False

        while self._running:
            try:
                if _settings_check_counter >= _SETTINGS_CHECK_INTERVAL:
                    settings = await self._get_settings()
                    _settings_check_counter = 0
                    if settings:
                        new_interval = settings.collection_interval or 60
                        if new_interval != self._collection_interval:
                            logger.info(f"Collection interval changed: {self._collection_interval}s -> {new_interval}s")
                            self._collection_interval = new_interval
                            if self._time_since_last_collect >= new_interval:
                                self._time_since_last_collect = new_interval
                        _cached_enabled = settings.enabled
                    else:
                        _cached_enabled = False

                if _cached_enabled and self._time_since_last_collect >= self._collection_interval:
                    await self._collect_ips_from_api()
                    self._time_since_last_collect = 0

                await asyncio.sleep(1)
                self._time_since_last_collect += 1
                _settings_check_counter += 1

            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"Collection error: {e}")
                await asyncio.sleep(5)
                self._time_since_last_collect += 5
                _settings_check_counter += 5

    async def _collect_ips_from_api(self):
        settings = await self._get_settings()
        if not settings or not settings.api_url or not settings.api_token:
            return

        api = get_remnawave_api(settings.api_url, settings.api_token, settings.cookie_secret)
        self._collecting = True
        try:
            nodes = await api.get_all_nodes()
            active_nodes = [
                n for n in nodes
                if n.get("isConnected") and not n.get("isDisabled")
            ]
            self._last_nodes_count = len(active_nodes)

            if not active_nodes:
                logger.debug("No active Remnawave nodes found")
                return

            sem = asyncio.Semaphore(IP_POLL_CONCURRENCY)

            async def _poll_node(node: dict) -> list[dict]:
                async with sem:
                    try:
                        return await api.poll_users_ips(node["uuid"])
                    except RemnawaveAPIError as e:
                        logger.debug(f"IP fetch failed for node {node.get('name', node['uuid'])}: {e.message}")
                        return []
                    except Exception as e:
                        logger.debug(f"IP fetch error for node {node.get('name', node['uuid'])}: {e}")
                        return []

            results = await asyncio.gather(*[_poll_node(n) for n in active_nodes])

            # Мержим результаты: (user_id, ip) -> last_seen
            merged: dict[tuple[int, str], datetime] = {}
            ignored = await self._get_ignored_user_ids()

            # Только ACTIVE пользователи
            async with async_session() as db:
                active_result = await db.execute(
                    select(RemnawaveUserCache.email).where(RemnawaveUserCache.status == 'ACTIVE')
                )
                active_user_ids = {row[0] for row in active_result.all()}

            now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
            freshness_cutoff = now_utc - timedelta(seconds=self._collection_interval)

            for node_users in results:
                for user_entry in node_users:
                    try:
                        user_id = int(user_entry.get("userId", "0"))
                    except (ValueError, TypeError):
                        continue
                    if not user_id or user_id in ignored or user_id not in active_user_ids:
                        continue
                    for ip_entry in user_entry.get("ips", []):
                        ip = ip_entry.get("ip", "")
                        if not ip:
                            continue
                        last_seen = self._parse_datetime(ip_entry.get("lastSeen"))
                        if not last_seen or last_seen < freshness_cutoff:
                            continue
                        key = (user_id, ip)
                        existing = merged.get(key)
                        if existing is None or last_seen > existing:
                            merged[key] = last_seen

            if merged:
                await self._save_stats(merged)

            self._last_collect_time = datetime.now(timezone.utc).replace(tzinfo=None)
            unique_users_with_ip = len({k[0] for k in merged})
            logger.info(
                f"IP collection: {len(active_nodes)} nodes, "
                f"{unique_users_with_ip} users with IPs, "
                f"{len(merged)} (user,ip) pairs, "
                f"{len(active_user_ids)} ACTIVE in cache"
            )

            # Проверка аномалий после каждого сбора
            try:
                await self._check_anomalies(settings)
            except Exception as e:
                logger.warning(f"Anomaly check failed: {e}")

        except RemnawaveAPIError as e:
            logger.warning(f"Remnawave API error during collection: {e.message}")
        except Exception as e:
            logger.error(f"Collection error: {e}")
        finally:
            self._collecting = False
            await api.close()

    async def collect_now(self) -> dict:
        settings = await self._get_settings()
        if not settings or not settings.enabled:
            return {"success": False, "error": "Collection is disabled", "collected_at": None}
        if self._collecting:
            return {"success": False, "error": "Collection already in progress", "collected_at": None}

        asyncio.create_task(self._collect_now_background())

        return {"success": True, "message": "Collection started"}

    async def _collect_now_background(self):
        try:
            await self._collect_ips_from_api()
            self._time_since_last_collect = 0
            await self._sync_hwid_devices()
        except Exception as e:
            logger.warning(f"Background collect failed: {e}")

    def get_status(self) -> dict:
        next_collect_in = None
        if self._running:
            next_collect_in = max(0, self._collection_interval - self._time_since_last_collect)
        return {
            "running": self._running,
            "collecting": self._collecting,
            "collection_interval": self._collection_interval,
            "last_collect_time": self._last_collect_time.isoformat() if self._last_collect_time else None,
            "next_collect_in": next_collect_in,
            "last_nodes_count": self._last_nodes_count,
        }

    async def _save_stats(self, merged: dict[tuple[int, str], datetime]):
        """Заменяет все IP-данные актуальным снимком (DELETE + INSERT)."""
        async with self._db_write_lock:
            async with async_session() as db:
                await db.execute(delete(XrayStats))

                items = list(merged.items())
                for i in range(0, len(items), UPSERT_BATCH_SIZE):
                    batch = items[i:i + UPSERT_BATCH_SIZE]
                    values = [
                        {"email": email, "source_ip": source_ip, "last_seen": last_seen}
                        for (email, source_ip), last_seen in batch
                    ]
                    await db.execute(pg_insert(XrayStats).values(values))

                await db.commit()

            logger.debug(f"Saved {len(merged)} current IP entries (full replace)")

    # === User cache ===

    async def _user_cache_loop(self):
        await asyncio.sleep(30)
        while self._running:
            try:
                settings = await self._get_settings()
                if settings and settings.enabled:
                    await self._update_user_cache()
                    await self._sync_hwid_devices()
                    try:
                        await self._check_traffic_anomalies(settings)
                    except Exception as e:
                        logger.warning(f"Traffic anomaly check failed: {e}")
                await asyncio.sleep(self._user_cache_interval)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"User cache error: {e}")
                await asyncio.sleep(300)

    async def _update_user_cache(self) -> dict:
        settings = await self._get_settings()
        if not settings or not settings.api_url or not settings.api_token:
            return {"success": False, "error": "API not configured", "count": 0}
        if self._user_cache_updating:
            return {"success": False, "error": "Update already in progress", "count": 0}

        self._user_cache_updating = True
        max_retries = 2
        last_error = None

        for attempt in range(1, max_retries + 1):
            api = get_remnawave_api(settings.api_url, settings.api_token, settings.cookie_secret)
            try:
                users = await api.get_all_users_paginated(size=200, concurrency=5)

                now = datetime.now(timezone.utc).replace(tzinfo=None)
                async with async_session() as db:
                    seen_ids: dict[int, dict] = {}
                    for user in users:
                        uid = user.get("id")
                        if uid is not None:
                            seen_ids[uid] = user

                    unique_users = list(seen_ids.values())
                    fetched_emails = set(seen_ids.keys())

                    if unique_users:
                        await self._batch_upsert_user_cache(db, unique_users, now)

                    # Удаляем пользователей, которых больше нет в Remnawave
                    # Вместо notin_ с 57k+ параметрами — вычисляем разницу в Python
                    if fetched_emails:
                        current_result = await db.execute(
                            select(RemnawaveUserCache.email)
                        )
                        current_emails = {row[0] for row in current_result.all()}
                        stale_emails = current_emails - fetched_emails
                        if stale_emails:
                            stale_list = list(stale_emails)
                            for i in range(0, len(stale_list), UPSERT_BATCH_SIZE):
                                batch = stale_list[i:i + UPSERT_BATCH_SIZE]
                                await db.execute(
                                    delete(RemnawaveUserCache).where(
                                        RemnawaveUserCache.email.in_(batch)
                                    )
                                )

                    await db.commit()

                self._last_user_cache_update = now
                active_count = sum(1 for u in unique_users if u.get("status") == "ACTIVE")
                logger.info(f"User cache synced: {len(unique_users)} total, {active_count} ACTIVE")
                self._user_cache_updating = False
                return {"success": True, "count": len(unique_users), "error": None}

            except RemnawaveAPIError as e:
                last_error = e.message
                logger.warning(f"User cache sync attempt {attempt}/{max_retries} failed: {e.message}")
            except Exception as e:
                last_error = str(e)
                logger.warning(f"User cache sync attempt {attempt}/{max_retries} error: {e}")
            finally:
                await api.close()

            if attempt < max_retries:
                await asyncio.sleep(5)

        self._user_cache_updating = False
        return {"success": False, "error": last_error, "count": 0}

    async def refresh_user_cache_now(self) -> dict:
        return await self._update_user_cache()

    def get_user_cache_status(self) -> dict:
        return {
            "last_update": self._last_user_cache_update.isoformat() if self._last_user_cache_update else None,
            "updating": self._user_cache_updating,
            "update_interval": self._user_cache_interval,
        }

    async def _batch_upsert_user_cache(self, db: AsyncSession, users: list[dict], now: datetime):
        values = []
        for user in users:
            user_id = user.get("id")
            if not user_id:
                continue
            user_traffic = user.get("userTraffic") or {}
            values.append({
                "email": user_id,
                "uuid": user.get("uuid"),
                "short_uuid": user.get("shortUuid"),
                "username": user.get("username"),
                "telegram_id": user.get("telegramId"),
                "status": user.get("status"),
                "expire_at": self._parse_datetime(user.get("expireAt")),
                "subscription_url": user.get("subscriptionUrl"),
                "sub_revoked_at": self._parse_datetime(user.get("subRevokedAt")),
                "traffic_limit_bytes": user.get("trafficLimitBytes"),
                "traffic_limit_strategy": user.get("trafficLimitStrategy"),
                "last_traffic_reset_at": self._parse_datetime(user.get("lastTrafficResetAt")),
                "used_traffic_bytes": user_traffic.get("usedTrafficBytes"),
                "lifetime_used_traffic_bytes": user_traffic.get("lifetimeUsedTrafficBytes"),
                "online_at": self._parse_datetime(user_traffic.get("onlineAt")),
                "first_connected_at": self._parse_datetime(user_traffic.get("firstConnectedAt")),
                "last_connected_node_uuid": user_traffic.get("lastConnectedNodeUuid"),
                "hwid_device_limit": user.get("hwidDeviceLimit"),
                "user_email": user.get("email"),
                "description": user.get("description"),
                "tag": user.get("tag"),
                "created_at": self._parse_datetime(user.get("createdAt")),
                "updated_at": now,
            })

        if not values:
            return

        update_cols = [
            'uuid', 'short_uuid', 'username', 'telegram_id', 'status',
            'expire_at', 'subscription_url', 'sub_revoked_at',
            'traffic_limit_bytes', 'traffic_limit_strategy',
            'last_traffic_reset_at', 'used_traffic_bytes', 'lifetime_used_traffic_bytes',
            'online_at', 'first_connected_at', 'last_connected_node_uuid',
            'hwid_device_limit', 'user_email', 'description', 'tag',
            'created_at', 'updated_at',
        ]

        for i in range(0, len(values), USER_CACHE_BATCH_SIZE):
            batch = values[i:i + USER_CACHE_BATCH_SIZE]
            try:
                async with db.begin_nested():
                    stmt = pg_insert(RemnawaveUserCache).values(batch)
                    stmt = stmt.on_conflict_do_update(
                        index_elements=['email'],
                        set_={col: getattr(stmt.excluded, col) for col in update_cols}
                    )
                    await db.execute(stmt)
            except Exception as e:
                logger.warning(f"User cache batch failed: {e}")

    # === HWID devices sync ===

    async def sync_hwid_now(self) -> dict:
        """Manual HWID sync trigger."""
        try:
            await self._sync_hwid_devices()
            return {"success": True}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def _sync_hwid_devices(self):
        settings = await self._get_settings()
        if not settings or not settings.api_url or not settings.api_token:
            logger.debug("HWID sync skipped: API not configured")
            return

        api = get_remnawave_api(settings.api_url, settings.api_token, settings.cookie_secret)
        try:
            devices = await api.get_all_hwid_devices_paginated(size=200, concurrency=5)
            logger.info(f"HWID API returned {len(devices)} devices")
            if not devices:
                return

            now = datetime.now(timezone.utc).replace(tzinfo=None)

            # Дедупликация по hwid — API может вернуть дубли
            unique: dict[str, dict] = {}
            for dev in devices:
                hwid = dev.get("hwid")
                if not hwid:
                    continue
                unique[hwid] = {
                    "hwid": hwid,
                    "user_uuid": dev.get("userUuid", ""),
                    "platform": dev.get("platform"),
                    "os_version": dev.get("osVersion"),
                    "device_model": dev.get("deviceModel"),
                    "user_agent": dev.get("userAgent"),
                    "created_at": self._parse_datetime(dev.get("createdAt")),
                    "updated_at": self._parse_datetime(dev.get("updatedAt")),
                    "synced_at": now,
                }
            values = list(unique.values())

            update_cols = [
                'user_uuid', 'platform', 'os_version', 'device_model',
                'user_agent', 'created_at', 'updated_at', 'synced_at',
            ]

            batch_size = 50
            async with async_session() as db:
                for i in range(0, len(values), batch_size):
                    batch = values[i:i + batch_size]
                    stmt = pg_insert(RemnawaveHwidDevice).values(batch)
                    stmt = stmt.on_conflict_do_update(
                        index_elements=['hwid'],
                        set_={col: getattr(stmt.excluded, col) for col in update_cols}
                    )
                    await db.execute(stmt)

                # Удаляем устройства, которых не было в этой синхронизации
                # (у них synced_at старее текущего now)
                await db.execute(
                    delete(RemnawaveHwidDevice).where(
                        RemnawaveHwidDevice.synced_at < now
                    )
                )

                await db.commit()

            logger.info(f"HWID devices synced: {len(values)} devices")

        except RemnawaveAPIError as e:
            logger.warning(f"HWID sync failed: {e.message}")
        except Exception as e:
            logger.error(f"HWID sync error: {e}")
        finally:
            await api.close()

    # === Anomaly detection ===

    _anomaly_last_notified: dict[str, datetime] = {}

    def _parse_id_list(self, json_str: str | None) -> set[int]:
        if not json_str:
            return set()
        try:
            data = json.loads(json_str)
            if isinstance(data, list):
                return {int(x) for x in data if str(x).isdigit()}
        except (json.JSONDecodeError, ValueError):
            pass
        return set()

    async def _get_bot_credentials(self) -> tuple[str | None, str | None]:
        async with async_session() as db:
            result = await db.execute(select(RemnawaveSettings).limit(1))
            settings = result.scalar_one_or_none()
            if not settings:
                return None, None

            if settings.anomaly_use_custom_bot and settings.anomaly_tg_bot_token and settings.anomaly_tg_chat_id:
                return settings.anomaly_tg_bot_token, settings.anomaly_tg_chat_id

            alert_result = await db.execute(select(AlertSettings).limit(1))
            alert = alert_result.scalar_one_or_none()
            if alert:
                return alert.telegram_bot_token, alert.telegram_chat_id
            return None, None

    async def _check_anomalies(self, settings: RemnawaveSettings):
        if not settings.anomaly_enabled:
            return

        bot_token, chat_id = await self._get_bot_credentials()
        if not bot_token or not chat_id:
            return

        ignore_ip = self._parse_id_list(settings.anomaly_ignore_ip)
        ignore_hwid = self._parse_id_list(settings.anomaly_ignore_hwid)
        ignored_all = await self._get_ignored_user_ids()
        now = datetime.now(timezone.utc).replace(tzinfo=None)

        async with async_session() as db:
            ip_rows = (await db.execute(
                select(XrayStats.email, sql_func.count(sql_func.distinct(XrayStats.source_ip)).label("cnt"))
                .group_by(XrayStats.email)
            )).all()

            ip_detail_rows = (await db.execute(
                select(XrayStats.email, XrayStats.source_ip)
            )).all()

            hwid_rows = (await db.execute(
                select(RemnawaveHwidDevice.user_uuid, sql_func.count().label("cnt"))
                .group_by(RemnawaveHwidDevice.user_uuid)
            )).all()

            device_rows = (await db.execute(
                select(RemnawaveHwidDevice.user_uuid, RemnawaveHwidDevice.user_agent,
                       RemnawaveHwidDevice.platform, RemnawaveHwidDevice.device_model,
                       RemnawaveHwidDevice.os_version, RemnawaveHwidDevice.hwid)
            )).all()

            cache_result = await db.execute(select(RemnawaveUserCache))
            cache_list = cache_result.scalars().all()
            by_email = {u.email: u for u in cache_list}
            by_uuid = {u.uuid: u for u in cache_list if u.uuid}

        ips_by_email: dict[int, list[str]] = {}
        for eid, sip in ip_detail_rows:
            ips_by_email.setdefault(eid, []).append(sip)

        COOLDOWN_SECONDS = 86400
        IP_CONFIRM_THRESHOLD = 5

        # 1) IP > лимит → уведомление с кнопкой [Игнор IP] (после 5 подтверждений подряд)
        current_anomaly_emails: set[int] = set()
        for email, ip_count in ip_rows:
            if email in ignored_all or email in ignore_ip:
                continue
            cached = by_email.get(email)
            if not cached or cached.status != 'ACTIVE':
                continue
            if not cached.hwid_device_limit or cached.hwid_device_limit <= 0:
                continue
            if ip_count <= cached.hwid_device_limit + 2:
                continue

            current_anomaly_emails.add(email)
            streak, _ = self._ip_anomaly_streak.get(email, (0, 0))
            streak += 1
            self._ip_anomaly_streak[email] = (streak, ip_count)

            if streak < IP_CONFIRM_THRESHOLD:
                continue

            # ASN-проверка: группируем IP по провайдеру
            user_ips = ips_by_email.get(email, [])
            asn_map = await lookup_ips_cached(user_ips) if user_ips else {}
            asn_groups = group_ips_by_asn(asn_map)
            unique_asn_count = effective_ip_count(asn_groups)

            if unique_asn_count <= cached.hwid_device_limit:
                logger.info(
                    f"IP anomaly suppressed by ASN: {cached.username or email} "
                    f"has {ip_count} IPs but {unique_asn_count} unique ASNs (limit: {cached.hwid_device_limit})"
                )
                continue

            key = f"ip:{email}"
            last = self._anomaly_last_notified.get(key)
            if last and (now - last).total_seconds() < COOLDOWN_SECONDS:
                continue
            self._anomaly_last_notified[key] = now
            self._ip_anomaly_streak.pop(email, None)

            # Обогащаем ASN-группы именами провайдеров для TG
            await enrich_with_names(asn_map)
            asn_groups = group_ips_by_asn(asn_map)

            lines = [
                "\u26a0\ufe0f <b>Аномалия: IP превышает лимит</b>\n",
                f"\ud83d\udc64 <b>{cached.username or f'#{email}'}</b> (ID: <code>{email}</code>)",
                f"\ud83c\udf10 IP: <b>{ip_count}</b> / {cached.hwid_device_limit} (ASN: <b>{unique_asn_count}</b>)",
                "",
            ]
            for group in asn_groups:
                asn_label = f"AS{group['asn']}" if group["asn"] else "Unknown"
                holder = group.get("holder") or ""
                header = f"{asn_label} {holder}".strip()
                lines.append(f"\ud83d\udccd <b>{header}</b> — {group['count']} IP")
                for ip in group["ips"][:5]:
                    lines.append(f"  \u2022 <code>{ip}</code>")
                if group["count"] > 5:
                    lines.append(f"  \u2022 ...\u0435\u0449\u0451 {group['count'] - 5}")

            keyboard = [[
                {"text": "\U0001f6ab Игнор IP", "callback_data": f"rw_ignore:ip:{email}"},
            ]]
            from app.services.telegram_bot import get_telegram_bot_service
            await get_telegram_bot_service().send_message(
                bot_token, chat_id, "\n".join(lines),
                reply_markup={"inline_keyboard": keyboard},
            )

        # Сброс streak для тех, кто вернулся в норму
        for email in list(self._ip_anomaly_streak):
            if email not in current_anomaly_emails:
                del self._ip_anomaly_streak[email]

        # 2) HWID > лимит → авто-очистка устройств через API (без уведомления)
        for uuid, device_count in hwid_rows:
            cached = by_uuid.get(uuid)
            if not cached or cached.status != 'ACTIVE':
                continue
            if cached.email in ignored_all or cached.email in ignore_hwid:
                continue
            if not cached.hwid_device_limit or cached.hwid_device_limit <= 0:
                continue
            if device_count <= cached.hwid_device_limit:
                continue
            key = f"hwid:{cached.email}"
            last = self._anomaly_last_notified.get(key)
            if last and (now - last).total_seconds() < COOLDOWN_SECONDS:
                continue
            self._anomaly_last_notified[key] = now
            await self._auto_clear_hwid(settings, uuid, cached.username, cached.email, device_count, cached.hwid_device_limit)

        # 3) Неизвестные UA → уведомление с кнопкой [Игнор HWID]
        # 4) Невалидные данные устройства (платформа/версия/модель)
        for uuid, user_agent, platform, device_model, os_version, hwid in device_rows:
            cached = by_uuid.get(uuid)
            if not cached or cached.status != 'ACTIVE':
                continue
            if cached.email in ignored_all or cached.email in ignore_hwid:
                continue

            # Проверка UA
            if user_agent and not KNOWN_UA_PATTERN.search(user_agent):
                key = f"ua:{cached.email}"
                last = self._anomaly_last_notified.get(key)
                if not last or (now - last).total_seconds() >= COOLDOWN_SECONDS:
                    self._anomaly_last_notified[key] = now
                    keyboard = [[
                        {"text": "\U0001f6ab Игнор HWID", "callback_data": f"rw_ignore:hwid:{cached.email}"},
                    ]]
                    from app.services.telegram_bot import get_telegram_bot_service
                    await get_telegram_bot_service().send_message(
                        bot_token, chat_id,
                        f"\ud83d\udd0d <b>Аномалия: Неизвестный User-Agent</b>\n\n"
                        f"\ud83d\udc64 <b>{cached.username or f'#{cached.email}'}</b> (ID: <code>{cached.email}</code>)\n"
                        f"\ud83d\udcbb {platform or '?'} / {device_model or '?'}\n"
                        f"<code>{user_agent[:80]}</code>",
                        reply_markup={"inline_keyboard": keyboard},
                    )

            # Проверка данных устройства: платформа, версия, модель — не пустые, версия в цифрах
            problems = []
            if not platform or not platform.strip():
                problems.append("платформа: пусто")
            if not os_version or not VERSION_PATTERN.match(os_version.strip()):
                problems.append(f"версия: «{os_version or 'пусто'}»")
            if not device_model or not device_model.strip():
                problems.append("модель: пусто")

            if problems:
                key = f"devdata:{hwid}"
                last = self._anomaly_last_notified.get(key)
                if last and (now - last).total_seconds() < COOLDOWN_SECONDS:
                    continue
                self._anomaly_last_notified[key] = now
                keyboard = [[
                    {"text": "\U0001f6ab Игнор HWID", "callback_data": f"rw_ignore:hwid:{cached.email}"},
                ]]
                from app.services.telegram_bot import get_telegram_bot_service
                await get_telegram_bot_service().send_message(
                    bot_token, chat_id,
                    f"\u26a0\ufe0f <b>Аномалия: Невалидные данные устройства</b>\n\n"
                    f"\ud83d\udc64 <b>{cached.username or f'#{cached.email}'}</b> (ID: <code>{cached.email}</code>)\n"
                    f"\ud83d\udcbb HWID: <code>{hwid[:40]}</code>\n"
                    f"\u274c {', '.join(problems)}",
                    reply_markup={"inline_keyboard": keyboard},
                )

    # === Traffic anomaly detection ===

    async def _check_traffic_anomalies(self, settings: RemnawaveSettings):
        if not settings.traffic_anomaly_enabled:
            return

        threshold_gb = settings.traffic_threshold_gb or 30.0
        confirm_count = settings.traffic_confirm_count or 2
        threshold_bytes = int(threshold_gb * 1024 * 1024 * 1024)

        bot_token, chat_id = await self._get_bot_credentials()
        if not bot_token or not chat_id:
            return

        ignored = await self._get_ignored_user_ids()
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        COOLDOWN_SECONDS = 86400

        async with async_session() as db:
            rows = (await db.execute(
                select(
                    RemnawaveUserCache.email,
                    RemnawaveUserCache.username,
                    RemnawaveUserCache.used_traffic_bytes,
                ).where(RemnawaveUserCache.status == 'ACTIVE')
            )).all()

        current_snapshot: dict[int, int] = {}
        username_map: dict[int, str | None] = {}
        for email, username, used_bytes in rows:
            if used_bytes is not None and used_bytes >= 0:
                current_snapshot[email] = used_bytes
                username_map[email] = username

        if not self._traffic_snapshot_initialized:
            self._traffic_snapshot = current_snapshot
            self._traffic_snapshot_initialized = True
            logger.info(f"Traffic snapshot initialized: {len(current_snapshot)} users")
            return

        active_emails: set[int] = set()
        for email, current_bytes in current_snapshot.items():
            if email in ignored:
                continue

            prev_bytes = self._traffic_snapshot.get(email)
            if prev_bytes is None:
                continue

            delta = max(0, current_bytes - prev_bytes)

            if delta >= threshold_bytes:
                active_emails.add(email)
                streak = self._traffic_anomaly_streak.get(email, 0) + 1
                self._traffic_anomaly_streak[email] = streak

                if streak >= confirm_count:
                    key = f"traffic:{email}"
                    last = self._anomaly_last_notified.get(key)
                    if last and (now - last).total_seconds() < COOLDOWN_SECONDS:
                        continue
                    self._anomaly_last_notified[key] = now
                    self._traffic_anomaly_streak.pop(email, None)

                    delta_gb = round(delta / (1024 ** 3), 2)
                    uname = username_map.get(email)
                    keyboard = [[
                        {"text": "\U0001f6ab Игнор", "callback_data": f"rw_ignore:all:{email}"},
                    ]]
                    from app.services.telegram_bot import get_telegram_bot_service
                    await get_telegram_bot_service().send_message(
                        bot_token, chat_id,
                        f"\U0001f4ca <b>Аномалия: Трафик превышает лимит</b>\n\n"
                        f"\U0001f464 <b>{uname or f'#{email}'}</b> (ID: <code>{email}</code>)\n"
                        f"\U0001f4e6 Дельта: <b>{delta_gb} GB</b> за 30 мин\n"
                        f"\u2699\ufe0f Порог: {threshold_gb} GB",
                        reply_markup={"inline_keyboard": keyboard},
                    )

        for email in list(self._traffic_anomaly_streak):
            if email not in active_emails:
                del self._traffic_anomaly_streak[email]

        self._traffic_snapshot = current_snapshot

    def get_traffic_anomalies(self) -> list[dict]:
        return [
            {"email": email, "streak": streak}
            for email, streak in self._traffic_anomaly_streak.items()
        ]

    def get_ip_anomaly_streaks(self) -> dict[int, tuple[int, int]]:
        """email -> (streak_count, ip_count)"""
        return dict(self._ip_anomaly_streak)

    async def _auto_clear_hwid(self, settings: RemnawaveSettings, user_uuid: str, username: str | None, email: int, current: int, limit: int):
        """Авто-очистка HWID устройств при превышении лимита."""
        api = get_remnawave_api(settings.api_url, settings.api_token, settings.cookie_secret)
        try:
            await api.delete_all_user_hwid_devices(user_uuid)
            logger.info(f"HWID auto-clear: {username or email} had {current}/{limit} devices, cleared")
        except Exception as e:
            logger.warning(f"HWID auto-clear failed for {username or email}: {e}")
        finally:
            await api.close()


    # === Cleanup ===

    async def _cleanup_loop(self):
        await self._cleanup_old_data()
        while self._running:
            try:
                await asyncio.sleep(3600)
                await self._cleanup_old_data()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"Cleanup error: {e}")

    async def _cleanup_old_data(self):
        """Очистка устаревших данных кэша (IP-данные эфемерные, чистятся при каждом сборе)."""
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        try:
            async with async_session() as db:
                cache_cutoff = now - timedelta(days=7)
                await db.execute(
                    delete(RemnawaveUserCache).where(RemnawaveUserCache.updated_at < cache_cutoff)
                )
                await db.execute(
                    delete(RemnawaveHwidDevice).where(RemnawaveHwidDevice.synced_at < cache_cutoff)
                )
                await db.commit()
        except Exception as e:
            logger.error(f"Cleanup error: {e}")

    # === Helpers ===

    def _parse_datetime(self, value: str | None) -> datetime | None:
        if not value:
            return None
        try:
            if value.endswith('Z'):
                value = value[:-1] + '+00:00'
            dt = datetime.fromisoformat(value)
            return dt.replace(tzinfo=None)
        except (ValueError, TypeError):
            return None


# --- aiogram Router для Telegram callback-кнопок ---

from aiogram import Router, F
from aiogram.types import CallbackQuery

_rw_callback_router = Router(name="remnawave_callbacks")


@_rw_callback_router.callback_query(F.data.startswith("rw_ignore:"))
async def handle_rw_ignore(callback: CallbackQuery):
    """Обработчик inline-кнопок игнорирования аномалий."""
    parts = callback.data.split(":")
    if len(parts) != 3:
        return

    _, list_type, user_id_str = parts
    if list_type not in ("ip", "hwid", "all"):
        return

    try:
        user_id = int(user_id_str)
    except ValueError:
        return

    from app.routers.remnawave import _modify_id_list
    async with async_session() as db:
        result = await db.execute(select(RemnawaveSettings).limit(1))
        s = result.scalar_one_or_none()
        if not s:
            return
        if list_type in ("ip", "all"):
            s.anomaly_ignore_ip = _modify_id_list(s.anomaly_ignore_ip, user_id, "add")
        if list_type in ("hwid", "all"):
            s.anomaly_ignore_hwid = _modify_id_list(s.anomaly_ignore_hwid, user_id, "add")
        if list_type == "all":
            s.ignored_user_ids = _modify_id_list(s.ignored_user_ids, user_id, "add")
        await db.commit()

    labels = {"ip": "IP", "hwid": "HWID", "all": "всех проверок"}
    await callback.answer(f"#{user_id} добавлен в игнор {labels.get(list_type, list_type)}")


# --- Singleton ---

_collector: Optional[XrayStatsCollector] = None


def get_xray_stats_collector() -> XrayStatsCollector:
    global _collector
    if _collector is None:
        _collector = XrayStatsCollector()
    return _collector


async def start_xray_stats_collector():
    collector = get_xray_stats_collector()
    await collector.start()


async def stop_xray_stats_collector():
    collector = get_xray_stats_collector()
    await collector.stop()
