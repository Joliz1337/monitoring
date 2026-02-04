"""
Traffic Anomaly Analyzer for Remnawave integration.

Analyzes users for suspicious activity:
- High traffic usage (exceeding configurable limit)
- IP count exceeding device limit (from XrayUserIpStats)
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
    RemnawaveUserCache, XrayUserIpStats, RemnawaveSettings
)
from app.services.remnawave_api import get_remnawave_api, RemnawaveAPIError

logger = logging.getLogger(__name__)

# Known valid platforms for HWID validation
VALID_PLATFORMS = {'android', 'ios', 'windows', 'macos', 'linux', 'mac'}

# Known valid app patterns
VALID_APP_PATTERNS = [
    r'^Happ/\d+\.\d+\.\d+/',
    r'^v2raytun/',
    r'^V2rayNG/',
    r'^Shadowrocket/',
    r'^Quantumult/',
    r'^Clash/',
    r'^Surge/',
    r'^FoXray/',
    r'^Streisand/',
    r'^OneClick/',
    r'^V2Box/',
    r'^Matsuri/',
    r'^NekoBox/',
    r'^SagerNet/',
    r'^V2RayU/',
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
                
                if settings and settings.enabled and self._time_since_last_check >= self._check_interval:
                    await self._run_analysis(settings)
                    self._time_since_last_check = 0
                
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
    
    async def _run_analysis(self, settings: TrafficAnalyzerSettings) -> dict:
        """Run full analysis on all users."""
        self._analyzing = True
        analyzed_users = 0
        anomalies_found = 0
        
        try:
            # Get Remnawave API settings for HWID fetching
            remnawave_settings = await self._get_remnawave_settings()
            
            # Get all users from cache
            async with async_session() as db:
                result = await db.execute(select(RemnawaveUserCache))
                users = result.scalars().all()
            
            if not users:
                logger.info("No users in cache to analyze")
                return {
                    "success": True,
                    "analyzed_users": 0,
                    "anomalies_found": 0
                }
            
            # 24h cutoff for IP stats
            cutoff_time = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=24)
            
            for user in users:
                try:
                    user_anomalies = await self._analyze_user(
                        user, settings, remnawave_settings, cutoff_time
                    )
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
        remnawave_settings: Optional[RemnawaveSettings],
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
        
        # 3. Check HWID (if enabled and API configured)
        if settings.check_hwid_anomalies and remnawave_settings and remnawave_settings.api_url:
            hwid_anomaly = await self._check_hwid_anomaly(user, remnawave_settings)
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
        """Check if user exceeds traffic limit."""
        used_bytes = user.used_traffic_bytes or 0
        limit_bytes = settings.traffic_limit_gb * (1024 ** 3)
        
        if used_bytes <= limit_bytes:
            return None
        
        used_gb = used_bytes / (1024 ** 3)
        severity = "critical" if used_bytes > limit_bytes * 2 else "warning"
        
        return {
            "type": "traffic",
            "severity": severity,
            "details": {
                "used_gb": round(used_gb, 2),
                "limit_gb": settings.traffic_limit_gb,
                "exceeded_by_gb": round(used_gb - settings.traffic_limit_gb, 2)
            }
        }
    
    async def _check_ip_anomaly(
        self,
        user: RemnawaveUserCache,
        settings: TrafficAnalyzerSettings,
        cutoff_time: datetime
    ) -> Optional[dict]:
        """Check if user has too many unique IPs."""
        device_limit = user.hwid_device_limit or 2
        ip_limit = int(device_limit * settings.ip_limit_multiplier)
        
        # Count unique non-infrastructure IPs in last 24h
        async with async_session() as db:
            result = await db.execute(
                select(func.count(func.distinct(XrayUserIpStats.source_ip)))
                .where(
                    and_(
                        XrayUserIpStats.email == user.email,
                        XrayUserIpStats.is_infrastructure == False,
                        XrayUserIpStats.last_seen >= cutoff_time
                    )
                )
            )
            unique_ips = result.scalar() or 0
        
        if unique_ips <= ip_limit:
            return None
        
        severity = "critical" if unique_ips > ip_limit * 1.5 else "warning"
        
        return {
            "type": "ip_count",
            "severity": severity,
            "details": {
                "unique_ips": unique_ips,
                "device_limit": device_limit,
                "ip_limit": ip_limit,
                "exceeded_by": unique_ips - ip_limit
            }
        }
    
    async def _check_hwid_anomaly(
        self,
        user: RemnawaveUserCache,
        remnawave_settings: RemnawaveSettings
    ) -> Optional[dict]:
        """Check HWID devices for suspicious User-Agent patterns."""
        if not user.uuid:
            return None
        
        api = get_remnawave_api(
            remnawave_settings.api_url,
            remnawave_settings.api_token,
            remnawave_settings.cookie_secret
        )
        
        try:
            hwid_data = await api.get_user_hwid_devices(user.uuid)
            if not hwid_data:
                return None
            
            devices = hwid_data.get("devices", [])
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
            
        except RemnawaveAPIError as e:
            logger.debug(f"Failed to fetch HWID for user {user.uuid}: {e.message}")
            return None
        finally:
            await api.close()
    
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
                message += f"\n📊 <b>Использовано:</b> {details['used_gb']} ГБ\n"
                message += f"📊 <b>Лимит:</b> {details['limit_gb']} ГБ\n"
                message += f"⚠️ <b>Превышение:</b> +{details['exceeded_by_gb']} ГБ"
            
            elif anomaly['type'] == 'ip_count':
                message += f"\n🌐 <b>Уникальных IP:</b> {details['unique_ips']}\n"
                message += f"📱 <b>Лимит устройств:</b> {details['device_limit']}\n"
                message += f"⚠️ <b>Превышение IP:</b> +{details['exceeded_by']}"
            
            elif anomaly['type'] == 'hwid':
                message += f"\n📱 <b>Всего устройств:</b> {details['total_devices']}\n"
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
