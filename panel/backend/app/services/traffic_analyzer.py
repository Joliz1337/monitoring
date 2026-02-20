"""
Traffic Anomaly Analyzer for Remnawave integration.

Analyzes users for suspicious activity:
- High traffic usage (exceeding configurable limit)
- IP count exceeding device limit (from XrayStats)
- Suspicious HWID/User-Agent patterns
"""

import asyncio
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

import aiohttp
from sqlalchemy import select, update, func, and_
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import async_session
from app.models import (
    TrafficAnalyzerSettings, TrafficAnomalyLog,
    RemnawaveUserCache, XrayStats, RemnawaveSettings,
    UserTrafficSnapshot
)
from app.services.remnawave_api import get_remnawave_api, RemnawaveAPIError

logger = logging.getLogger(__name__)

# Minimum total visit count for an ASN group to be considered active
MIN_ASN_VISIT_COUNT = 1000

# Known valid platforms for HWID validation
VALID_PLATFORMS = {'android', 'ios', 'windows', 'macos', 'linux', 'mac'}

# Known valid app patterns (case-insensitive)
# No $ anchor — real user-agents often contain OS/framework info after app name
# e.g. "Shadowrocket/1882 CFNetwork/1568.200.51 Darwin/24.1.0"
VALID_APP_PATTERNS = [
    # V2Ray family
    r'^V2rayNG',                           # V2rayNG, V2rayNG/1.8.5
    r'^v2raytun',                          # v2rayTUN/2.0.5(Build 207) CFNetwork/...
    r'^v2rayA',                            # v2rayA
    r'^V2RayU',                            # V2RayU (macOS)
    r'^V2Box',                             # V2Box
    r'^Qv2ray',                            # Qv2ray
    r'^Happ',                              # Happ/3.9.1/Windows
    # Shadowrocket / Quantumult / Surge / Loon / Stash (iOS/macOS)
    r'^Shadowrocket',                      # Shadowrocket/1882 CFNetwork/...
    r'^Quantumult',                        # Quantumult X, Quantumult%20X/1.4.1
    r'^Surge',                             # Surge/2922 CFNetwork/...
    r'^Loon',                              # Loon/622 CFNetwork/...
    r'^Stash',                             # Stash/2.5.0 CFNetwork/...
    r'^Pharos',                            # Pharos
    r'^Spectre',                           # Spectre VPN
    r'^FoXray',                            # FoXray
    # Clash / Mihomo family
    r'^Clash',                             # Clash, ClashX, ClashForAndroid, clash-verge, clash-nyanpasu, clash.meta
    r'^FlClash',                           # FlClash, FlClashX
    r'^Flowvy',                            # Flowvy (Mihomo client)
    r'^mihomo',                            # Mihomo core
    r'^koala[\-_]?clash',                  # koala-clash, KoalaClash
    r'^murge',                             # Murge (Mihomo GUI)
    r'^prizrak[\-_]?box',                  # prizrak-box
    # sing-box family
    r'^sing[\-]?box',                      # sing-box, singbox
    r'^sf[aimt]([/ \d]|$)',                # SFA, SFI, SFM, SFT (sing-box platform clients)
    r'^karing',                            # Karing (sing-box GUI)
    r'^rabbithole',                        # RabbitHole
    # NekoBox / Nekoray / SagerNet / Matsuri
    r'^NekoBox',                           # NekoBox, NekoBoxForAndroid
    r'^nekoray',                           # Nekoray
    r'^SagerNet',                          # SagerNet
    r'^Matsuri',                           # Matsuri
    # Other clients
    r'^Streisand',                         # Streisand (iOS)
    r'^OneClick',                          # OneClick
    r'^hiddify',                           # Hiddify, HiddifyNext
    r'^WingsX',                            # WingsX
]

# Suspicious patterns in User-Agent
SUSPICIOUS_PATTERNS = [
    r'GAYNETWORK',
    r'FREE',
    r'CRACK',
    r'HACK',
    r'PIRATE',
    r'STOLEN',
    r'SHARED',
]


def validate_user_agent(user_agent: str) -> dict:
    """
    Validate HWID User-Agent string.
    
    Returns:
        dict with 'valid' boolean and 'issues' list
    """
    issues = []
    
    if not user_agent or not user_agent.strip():
        return {'valid': False, 'issues': ['empty_user_agent']}
    
    ua = user_agent.strip()
    
    # Check for suspicious patterns
    for pattern in SUSPICIOUS_PATTERNS:
        if re.search(pattern, ua, re.IGNORECASE):
            issues.append(f'suspicious_pattern:{pattern}')
    
    # Check if matches any known valid app pattern
    matches_valid_app = False
    for pattern in VALID_APP_PATTERNS:
        if re.match(pattern, ua, re.IGNORECASE):
            matches_valid_app = True
            break
    
    if not matches_valid_app:
        # Check if has any platform identifier
        ua_lower = ua.lower()
        has_platform = any(p in ua_lower for p in VALID_PLATFORMS)
        if not has_platform:
            issues.append('unknown_app_format')
    
    # Check for very short user agents (likely fake)
    if len(ua) < 5:
        issues.append('too_short')
    
    return {
        'valid': len(issues) == 0,
        'issues': issues
    }


class TrafficAnalyzer:
    """Analyzes traffic anomalies for Remnawave users."""
    
    def __init__(self):
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._analyzing = False
        self._time_since_last_check = 0
        self._check_interval = 1800  # 30 minutes default
        self._last_check_time: Optional[datetime] = None
        self._hwid_cache: dict[str, list[dict]] = {}  # uuid -> devices list
        self._hwid_cache_updated: Optional[datetime] = None
    
    async def start(self):
        """Start background analyzer."""
        if self._running:
            return
        
        self._running = True
        self._task = asyncio.create_task(self._analysis_loop())
        logger.info("Traffic analyzer started")
    
    async def stop(self):
        """Stop background analyzer."""
        self._running = False
        
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        
        logger.info("Traffic analyzer stopped")
    
    async def _get_settings(self) -> Optional[TrafficAnalyzerSettings]:
        """Get analyzer settings from DB."""
        async with async_session() as db:
            result = await db.execute(select(TrafficAnalyzerSettings).limit(1))
            return result.scalar_one_or_none()
    
    async def _get_remnawave_settings(self) -> Optional[RemnawaveSettings]:
        """Get Remnawave API settings from DB."""
        async with async_session() as db:
            result = await db.execute(select(RemnawaveSettings).limit(1))
            return result.scalar_one_or_none()
    
    async def _analysis_loop(self):
        """Main analysis loop."""
        first_run = True
        self._time_since_last_check = 0
        
        while self._running:
            try:
                settings = await self._get_settings()
                
                if settings:
                    new_interval = (settings.check_interval_minutes or 30) * 60
                    if new_interval != self._check_interval:
                        logger.info(f"Analyzer interval changed: {self._check_interval}s -> {new_interval}s")
                        self._check_interval = new_interval
                        if self._time_since_last_check >= new_interval:
                            self._time_since_last_check = new_interval
                
                # Run immediately on first start if enabled, then on interval
                should_run = settings and settings.enabled and (
                    first_run or self._time_since_last_check >= self._check_interval
                )
                
                if should_run:
                    logger.info(f"Running analysis (first_run={first_run})")
                    await self._run_analysis(settings)
                    self._time_since_last_check = 0
                    first_run = False
                
                await asyncio.sleep(1)
                self._time_since_last_check += 1
                
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"Analyzer error: {e}")
                await asyncio.sleep(30)
                self._time_since_last_check += 30
    
    async def analyze_now(self) -> dict:
        """Force immediate analysis."""
        settings = await self._get_settings()
        
        if not settings:
            return {
                "success": False,
                "error": "Analyzer settings not configured",
                "analyzed_users": 0,
                "anomalies_found": 0
            }
        
        if not settings.enabled:
            return {
                "success": False,
                "error": "Analyzer is disabled",
                "analyzed_users": 0,
                "anomalies_found": 0
            }
        
        if self._analyzing:
            return {
                "success": False,
                "error": "Analysis already in progress",
                "analyzed_users": 0,
                "anomalies_found": 0
            }
        
        result = await self._run_analysis(settings)
        self._time_since_last_check = 0
        return result
    
    def get_status(self) -> dict:
        """Get analyzer status."""
        next_check_in = None
        if self._running:
            next_check_in = max(0, self._check_interval - self._time_since_last_check)
        
        return {
            "running": self._running,
            "analyzing": self._analyzing,
            "check_interval": self._check_interval,
            "last_check_time": self._last_check_time.isoformat() if self._last_check_time else None,
            "next_check_in": next_check_in
        }
    
    async def _get_ignored_user_ids(self) -> set[int]:
        """Get set of ignored user IDs from Remnawave settings."""
        import json
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
    
    async def _run_analysis(self, settings: TrafficAnalyzerSettings) -> dict:
        """Run full analysis on all users."""
        self._analyzing = True
        analyzed_users = 0
        anomalies_found = 0
        
        try:
            # Get Remnawave API settings for HWID fetching
            remnawave_settings = await self._get_remnawave_settings()
            
            # Get ignored user IDs (excluded from analysis and notifications)
            ignored_user_ids = await self._get_ignored_user_ids()
            if ignored_user_ids:
                logger.debug(f"Excluding {len(ignored_user_ids)} ignored users from analysis")
            
            # Get all users from cache
            async with async_session() as db:
                result = await db.execute(select(RemnawaveUserCache))
                all_users = result.scalars().all()
            
            # Filter out ignored users
            users = [u for u in all_users if u.email not in ignored_user_ids]
            
            if not users:
                logger.info("No users in cache to analyze (after filtering)")
                return {
                    "success": True,
                    "analyzed_users": 0,
                    "anomalies_found": 0,
                    "ignored_users": len(ignored_user_ids)
                }
            
            # Pre-fetch all HWID devices in one batch (instead of per-user requests)
            if settings.check_hwid_anomalies and remnawave_settings and remnawave_settings.api_url:
                await self._refresh_hwid_cache(remnawave_settings)
            
            # 24h cutoff for IP stats
            cutoff_time = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=24)
            
            for user in users:
                try:
                    user_anomalies = await self._analyze_user(user, settings, cutoff_time)
                    analyzed_users += 1
                    anomalies_found += len(user_anomalies)
                except Exception as e:
                    logger.debug(f"Error analyzing user {user.email}: {e}")
            
            # Update last check time
            now = datetime.now(timezone.utc).replace(tzinfo=None)
            self._last_check_time = now
            
            async with async_session() as db:
                await db.execute(
                    update(TrafficAnalyzerSettings)
                    .where(TrafficAnalyzerSettings.id == settings.id)
                    .values(last_check_at=now, last_error=None)
                )
                await db.commit()
            
            logger.info(f"Analysis complete: {analyzed_users} users, {anomalies_found} anomalies")
            
            return {
                "success": True,
                "analyzed_users": analyzed_users,
                "anomalies_found": anomalies_found
            }
            
        except Exception as e:
            error_msg = str(e)[:500]
            logger.error(f"Analysis failed: {error_msg}")
            
            async with async_session() as db:
                await db.execute(
                    update(TrafficAnalyzerSettings)
                    .where(TrafficAnalyzerSettings.id == settings.id)
                    .values(last_error=error_msg)
                )
                await db.commit()
            
            return {
                "success": False,
                "error": error_msg,
                "analyzed_users": analyzed_users,
                "anomalies_found": anomalies_found
            }
        finally:
            self._analyzing = False
    
    async def _analyze_user(
        self,
        user: RemnawaveUserCache,
        settings: TrafficAnalyzerSettings,
        cutoff_time: datetime
    ) -> list[dict]:
        """Analyze single user for anomalies."""
        anomalies = []
        
        # 1. Check traffic usage
        traffic_anomaly = await self._check_traffic_anomaly(user, settings)
        if traffic_anomaly:
            anomalies.append(traffic_anomaly)
        
        # 2. Check IP count
        ip_anomaly = await self._check_ip_anomaly(user, settings, cutoff_time)
        if ip_anomaly:
            anomalies.append(ip_anomaly)
        
        # 3. Check HWID (uses pre-fetched cache)
        if settings.check_hwid_anomalies and self._hwid_cache:
            hwid_anomaly = await self._check_hwid_anomaly(user)
            if hwid_anomaly:
                anomalies.append(hwid_anomaly)
        
        # Process anomalies
        for anomaly in anomalies:
            await self._save_and_notify_anomaly(user, anomaly, settings)
        
        return anomalies
    
    async def _check_traffic_anomaly(
        self,
        user: RemnawaveUserCache,
        settings: TrafficAnalyzerSettings
    ) -> Optional[dict]:
        """Check if user exceeds traffic limit within check interval.
        
        Compares current traffic with previous snapshot to calculate
        traffic consumed during the check interval (e.g., 30 minutes).
        """
        current_bytes = user.used_traffic_bytes or 0
        limit_bytes = settings.traffic_limit_gb * (1024 ** 3)
        check_interval_minutes = settings.check_interval_minutes or 30
        
        async with async_session() as db:
            # Get previous snapshot
            result = await db.execute(
                select(UserTrafficSnapshot).where(
                    UserTrafficSnapshot.user_email == user.email
                )
            )
            snapshot = result.scalar_one_or_none()
            
            previous_bytes = 0
            if snapshot:
                previous_bytes = snapshot.traffic_bytes or 0
                # Update existing snapshot
                snapshot.traffic_bytes = current_bytes
                snapshot.snapshot_at = datetime.now(timezone.utc).replace(tzinfo=None)
            else:
                # Create new snapshot (first check for this user)
                new_snapshot = UserTrafficSnapshot(
                    user_email=user.email,
                    traffic_bytes=current_bytes
                )
                db.add(new_snapshot)
            
            await db.commit()
        
        # Calculate traffic consumed during period
        # Handle case when traffic counter was reset (new billing period)
        if current_bytes < previous_bytes:
            # Traffic was reset, use current value as consumed
            consumed_bytes = current_bytes
        else:
            consumed_bytes = current_bytes - previous_bytes
        
        # Skip if no previous snapshot (first run, need baseline)
        if not snapshot:
            logger.debug(f"First snapshot for user {user.email}, skipping anomaly check")
            return None
        
        if consumed_bytes <= limit_bytes:
            return None
        
        consumed_gb = consumed_bytes / (1024 ** 3)
        limit_gb = settings.traffic_limit_gb
        severity = "critical" if consumed_bytes > limit_bytes * 2 else "warning"
        
        return {
            "type": "traffic",
            "severity": severity,
            "details": {
                "consumed_gb": round(consumed_gb, 2),
                "period_minutes": check_interval_minutes,
                "limit_gb": limit_gb,
                "exceeded_by_gb": round(consumed_gb - limit_gb, 2)
            }
        }
    
    async def _check_ip_anomaly(
        self,
        user: RemnawaveUserCache,
        settings: TrafficAnalyzerSettings,
        cutoff_time: datetime
    ) -> Optional[dict]:
        """Check if user has too many unique IP groups (by ASN) in last 24h.
        
        1. Get all IPs with visit counts for user (last 24h, excluding infra)
        2. Resolve ASN for each IP
        3. Group by ASN, sum visits per group
        4. Filter: only ASN-groups with >= MIN_ASN_VISIT_COUNT total visits are "active"
        5. Compare active group count with device limit
        """
        from app.services.asn_lookup import lookup_ips, group_ips_by_asn_with_visits, effective_ip_count
        
        device_limit = user.hwid_device_limit or 2
        ip_limit = int(device_limit * settings.ip_limit_multiplier)
        
        async with async_session() as db:
            from app.services.xray_stats_collector import _get_infrastructure_ips_sql
            infra_ips = await _get_infrastructure_ips_sql(db)
            
            conditions = [XrayStats.email == user.email, XrayStats.last_seen >= cutoff_time]
            if infra_ips:
                conditions.append(XrayStats.source_ip.notin_(list(infra_ips)))
            
            # Get all IPs with their visit counts (no per-IP threshold)
            ip_visits_query = (
                select(XrayStats.source_ip, func.sum(XrayStats.count).label('visits'))
                .where(and_(*conditions))
                .group_by(XrayStats.source_ip)
            )
            
            result = await db.execute(ip_visits_query)
            ip_visits = {row[0]: int(row[1]) for row in result.fetchall()}
        
        if not ip_visits:
            return None
        
        # Resolve ASN for each IP
        asn_map = await lookup_ips(list(ip_visits.keys()))
        
        # Group by ASN with visit counts, filter by MIN_ASN_VISIT_COUNT
        asn_groups = group_ips_by_asn_with_visits(asn_map, ip_visits, MIN_ASN_VISIT_COUNT)
        eff_count = effective_ip_count(asn_groups)
        
        if eff_count <= ip_limit:
            return None
        
        unique_ips = sum(g["count"] for g in asn_groups)
        severity = "critical" if eff_count > ip_limit * 1.5 else "warning"
        
        # Limit IPs in each group to 10 for notification compactness
        compact_groups = []
        for g in asn_groups:
            compact_groups.append({
                "asn": g["asn"],
                "prefix": g["prefix"],
                "ips": g["ips"][:10],
                "count": g["count"],
                "visits": g["visits"]
            })
        
        return {
            "type": "ip_count",
            "severity": severity,
            "details": {
                "unique_ips": unique_ips,
                "unique_asns": sum(1 for g in asn_groups if g["asn"] is not None),
                "effective_count": eff_count,
                "device_limit": device_limit,
                "ip_limit": ip_limit,
                "exceeded_by": eff_count - ip_limit,
                "min_visit_threshold": MIN_ASN_VISIT_COUNT,
                "asn_groups": compact_groups
            }
        }
    
    @staticmethod
    def _parse_device_datetime(value: str | None) -> datetime | None:
        if not value:
            return None
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return dt.replace(tzinfo=None) if dt.tzinfo else dt
        except (ValueError, AttributeError):
            return None

    async def _refresh_hwid_cache(self, remnawave_settings: RemnawaveSettings):
        """
        Fetch all HWID devices in one batch and cache by user UUID.
        Only devices created/updated in the last 24h are included.
        """
        api = get_remnawave_api(
            remnawave_settings.api_url,
            remnawave_settings.api_token,
            remnawave_settings.cookie_secret
        )
        
        try:
            all_devices = await api.get_all_hwid_devices_paginated(size=100)
            cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=24)
            
            self._hwid_cache.clear()
            filtered_count = 0
            for device in all_devices:
                updated = self._parse_device_datetime(device.get("updatedAt"))
                created = self._parse_device_datetime(device.get("createdAt"))
                latest = updated or created
                if latest and latest < cutoff:
                    continue
                
                filtered_count += 1
                user_uuid = device.get("userUuid")
                if user_uuid:
                    if user_uuid not in self._hwid_cache:
                        self._hwid_cache[user_uuid] = []
                    self._hwid_cache[user_uuid].append(device)
            
            self._hwid_cache_updated = datetime.now(timezone.utc).replace(tzinfo=None)
            logger.debug(f"HWID cache refreshed: {filtered_count}/{len(all_devices)} devices (24h) for {len(self._hwid_cache)} users")
            
        except RemnawaveAPIError as e:
            logger.warning(f"Failed to refresh HWID cache: {e.message}")
        finally:
            await api.close()
    
    async def _check_hwid_anomaly(self, user: RemnawaveUserCache) -> Optional[dict]:
        """Check HWID devices for suspicious User-Agent patterns using cached data."""
        if not user.uuid:
            return None
        
        # Use cached HWID data instead of making API request per user
        devices = self._hwid_cache.get(user.uuid, [])
        if not devices:
            return None
        
        suspicious_devices = []
        
        for device in devices:
            user_agent = device.get("userAgent", "")
            validation = validate_user_agent(user_agent)
            
            if not validation['valid']:
                suspicious_devices.append({
                    "hwid": device.get("hwid", "")[:20] + "...",
                    "user_agent": user_agent[:100] if user_agent else "(empty)",
                    "issues": validation['issues']
                })
        
        if not suspicious_devices:
            return None
        
        severity = "critical" if len(suspicious_devices) > 1 else "warning"
        
        return {
            "type": "hwid",
            "severity": severity,
            "details": {
                "total_devices": len(devices),
                "suspicious_count": len(suspicious_devices),
                "suspicious_devices": suspicious_devices[:5]  # Limit to 5
            }
        }
    
    async def _save_and_notify_anomaly(
        self,
        user: RemnawaveUserCache,
        anomaly: dict,
        settings: TrafficAnalyzerSettings
    ):
        """Save anomaly to DB and send notification."""
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        
        # Check if similar anomaly was already logged in last 24h
        cutoff = now - timedelta(hours=24)
        
        async with async_session() as db:
            result = await db.execute(
                select(TrafficAnomalyLog).where(
                    and_(
                        TrafficAnomalyLog.user_email == user.email,
                        TrafficAnomalyLog.anomaly_type == anomaly['type'],
                        TrafficAnomalyLog.created_at >= cutoff,
                        TrafficAnomalyLog.resolved == False
                    )
                )
            )
            existing = result.scalar_one_or_none()
            
            if existing:
                # Don't duplicate within 24h
                return
            
            # Create new anomaly log
            log_entry = TrafficAnomalyLog(
                user_email=user.email,
                username=user.username,
                anomaly_type=anomaly['type'],
                severity=anomaly['severity'],
                details=json.dumps(anomaly['details'], ensure_ascii=False),
                notified=False,
                resolved=False
            )
            db.add(log_entry)
            await db.commit()
            await db.refresh(log_entry)
        
        # Send Telegram notification
        if settings.telegram_bot_token and settings.telegram_chat_id:
            notified = await self._send_telegram_notification(user, anomaly, settings)
            
            if notified:
                async with async_session() as db:
                    await db.execute(
                        update(TrafficAnomalyLog)
                        .where(TrafficAnomalyLog.id == log_entry.id)
                        .values(notified=True)
                    )
                    await db.commit()
    
    async def _send_telegram_notification(
        self,
        user: RemnawaveUserCache,
        anomaly: dict,
        settings: TrafficAnalyzerSettings
    ) -> bool:
        """Send Telegram notification about anomaly."""
        try:
            # Build message
            severity_emoji = "🔴" if anomaly['severity'] == 'critical' else "🟡"
            type_names = {
                'traffic': 'Превышение трафика',
                'ip_count': 'Много IP адресов',
                'hwid': 'Подозрительные устройства'
            }
            
            message = f"{severity_emoji} <b>{type_names.get(anomaly['type'], anomaly['type'])}</b>\n\n"
            message += f"👤 <b>Пользователь:</b> {user.username or user.email}\n"
            
            if user.telegram_id:
                message += f"📱 Telegram ID: <code>{user.telegram_id}</code>\n"
            
            details = anomaly['details']
            
            if anomaly['type'] == 'traffic':
                period = details.get('period_minutes', 30)
                message += f"\n📊 <b>Потрачено за {period} мин:</b> {details['consumed_gb']} ГБ\n"
                message += f"📊 <b>Лимит на период:</b> {details['limit_gb']} ГБ\n"
                message += f"⚠️ <b>Превышение:</b> +{details['exceeded_by_gb']} ГБ"
            
            elif anomaly['type'] == 'ip_count':
                eff = details.get('effective_count', details['unique_ips'])
                message += f"\n🌐 <b>Уникальных IP:</b> {details['unique_ips']}\n"
                message += f"🏢 <b>ASN-групп:</b> {eff} (лимит: {details['ip_limit']})\n"
                message += f"📱 <b>Лимит устройств:</b> {details['device_limit']}\n"
                message += f"⚠️ <b>Превышение:</b> +{details['exceeded_by']}\n"
                
                for group in details.get('asn_groups', [])[:5]:
                    asn = group.get('asn') or '???'
                    prefix = group.get('prefix') or ''
                    count = group.get('count', 0)
                    visits = group.get('visits', 0)
                    prefix_str = f" ({prefix})" if prefix else ""
                    message += f"\n• ASN {asn}{prefix_str}: {count} IP, {visits} визитов"
            
            elif anomaly['type'] == 'hwid':
                message += f"\n📱 <b>Устройств за 24ч:</b> {details['total_devices']}\n"
                message += f"⚠️ <b>Подозрительных:</b> {details['suspicious_count']}\n"
                
                for device in details.get('suspicious_devices', [])[:3]:
                    issues_str = ', '.join(device['issues'])
                    message += f"\n• <code>{device['user_agent']}</code>\n  Проблемы: {issues_str}"
            
            # Send via Telegram API
            url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage"
            
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json={
                    "chat_id": settings.telegram_chat_id,
                    "text": message,
                    "parse_mode": "HTML"
                }) as response:
                    if response.status == 200:
                        logger.debug(f"Telegram notification sent for user {user.email}")
                        return True
                    else:
                        response_text = await response.text()
                        logger.warning(f"Telegram notification failed: {response.status} - {response_text}")
                        return False
                        
        except Exception as e:
            logger.error(f"Failed to send Telegram notification: {e}")
            return False
    
    async def test_telegram(self, bot_token: str, chat_id: str) -> dict:
        """Test Telegram configuration."""
        try:
            url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
            
            message = "✅ <b>Тест уведомлений</b>\n\nАнализатор трафика настроен корректно!"
            
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json={
                    "chat_id": chat_id,
                    "text": message,
                    "parse_mode": "HTML"
                }) as response:
                    if response.status == 200:
                        return {"success": True, "message": "Test message sent"}
                    else:
                        response_data = await response.json()
                        error_desc = response_data.get('description', 'Unknown error')
                        return {"success": False, "error": error_desc}
                        
        except Exception as e:
            return {"success": False, "error": str(e)}


# Singleton instance
_analyzer: Optional[TrafficAnalyzer] = None


def get_traffic_analyzer() -> TrafficAnalyzer:
    """Get or create Traffic Analyzer instance."""
    global _analyzer
    if _analyzer is None:
        _analyzer = TrafficAnalyzer()
    return _analyzer


async def start_traffic_analyzer():
    """Start the Traffic Analyzer."""
    analyzer = get_traffic_analyzer()
    await analyzer.start()


async def stop_traffic_analyzer():
    """Stop the Traffic Analyzer."""
    analyzer = get_traffic_analyzer()
    await analyzer.stop()
