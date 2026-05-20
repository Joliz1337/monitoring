import asyncio
import json
import logging
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse
from typing import Optional

from sqlalchemy import select, delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import async_session
from app.models import Server, AlertSettings, AlertHistory
from app.services.http_client import get_node_client, node_auth_headers

ALERT_HISTORY_RETENTION_DAYS = 30

logger = logging.getLogger(__name__)

EMA_WINDOW = 30
EMA_ALPHA = 2.0 / (EMA_WINDOW + 1)

MIN_EMA_SAMPLES = 5


class ServerAlertState:
    """In-memory state per server for adaptive thresholds and cooldowns."""

    def __init__(self):
        self.was_offline: bool = False
        self.samples: int = 0

        self.ema_cpu: float = 0.0
        self.ema_ram: float = 0.0
        self.ema_net_rx: float = 0.0
        self.ema_net_tx: float = 0.0
        self.ema_tcp_established: float = 0.0
        self.ema_tcp_listen: float = 0.0
        self.ema_tcp_timewait: float = 0.0
        self.ema_tcp_closewait: float = 0.0
        self.ema_tcp_synsent: float = 0.0
        self.ema_tcp_synrecv: float = 0.0
        self.ema_tcp_finwait: float = 0.0

        self.prev_net_rx: float = 0.0
        self.prev_net_tx: float = 0.0
        self.prev_time: float = 0.0
        self.net_initialized: bool = False

        self.load_avg_fail_count: int = 0

        self.alert_start: dict[str, float] = {}
        self.last_alert: dict[str, float] = {}

    def update_ema(self, attr: str, value: float):
        current = getattr(self, attr)
        if self.samples < 2:
            setattr(self, attr, value)
        else:
            setattr(self, attr, current * (1 - EMA_ALPHA) + value * EMA_ALPHA)

    def is_warmed(self) -> bool:
        return self.samples >= MIN_EMA_SAMPLES


class ServerAlerter:
    """Background service that monitors server health and sends Telegram alerts."""

    def __init__(self):
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._states: dict[int, ServerAlertState] = {}
        self._check_interval = 60
        self._last_check: Optional[datetime] = None
        self._time_since_check = 0

    async def start(self):
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop())
        logger.info("Server alerter started")

    async def stop(self):
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("Server alerter stopped")

    def get_status(self) -> dict:
        next_check = None
        if self._running:
            next_check = max(0, self._check_interval - self._time_since_check)
        active_alerts: dict[int, list[str]] = {}
        for sid, st in self._states.items():
            ongoing = [k for k, v in st.alert_start.items() if v]
            if ongoing:
                active_alerts[sid] = ongoing
        return {
            "running": self._running,
            "last_check": self._last_check.isoformat() if self._last_check else None,
            "next_check_in": next_check,
            "monitored_servers": len(self._states),
            "active_conditions": active_alerts,
        }

    # ------------------------------------------------------------------
    async def _loop(self):
        self._time_since_check = 0
        self._last_cleanup: float = 0
        first_run = True

        while self._running:
            try:
                settings = await self._load_settings()
                if settings:
                    self._check_interval = max(10, settings.check_interval or 60)

                should_run = settings and settings.enabled and (
                    first_run or self._time_since_check >= self._check_interval
                )

                if should_run:
                    await self._check_all(settings)
                    self._time_since_check = 0
                    first_run = False
                    
                    # Cleanup old history once per hour
                    now = time.time()
                    if now - self._last_cleanup >= 3600:
                        await self._cleanup_old_history()
                        self._last_cleanup = now

                await asyncio.sleep(1)
                self._time_since_check += 1
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"Alerter loop error: {e}")
                await asyncio.sleep(10)
                self._time_since_check += 10
    
    async def _cleanup_old_history(self):
        """Remove alert history older than retention period."""
        try:
            cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=ALERT_HISTORY_RETENTION_DAYS)
            async with async_session() as db:
                result = await db.execute(
                    delete(AlertHistory).where(AlertHistory.created_at < cutoff)
                )
                if result.rowcount > 0:
                    logger.info(f"Cleaned {result.rowcount} old alert history entries")
                await db.commit()
        except Exception as e:
            logger.debug(f"Alert history cleanup error: {e}")

    async def _load_settings(self) -> Optional[AlertSettings]:
        async with async_session() as db:
            result = await db.execute(select(AlertSettings).limit(1))
            return result.scalar_one_or_none()

    # ------------------------------------------------------------------
    @staticmethod
    def _parse_id_list(raw: str | None) -> set[int]:
        if not raw:
            return set()
        try:
            return set(json.loads(raw))
        except (json.JSONDecodeError, TypeError):
            return set()

    async def _check_all(self, settings: AlertSettings):
        now = time.time()
        self._last_check = datetime.now(timezone.utc).replace(tzinfo=None)

        excluded_ids = self._parse_id_list(settings.excluded_server_ids)

        self._trigger_excluded = {
            "offline": self._parse_id_list(settings.offline_excluded_server_ids),
            "cpu": self._parse_id_list(settings.cpu_excluded_server_ids),
            "ram": self._parse_id_list(settings.ram_excluded_server_ids),
            "network": self._parse_id_list(settings.network_excluded_server_ids),
            "tcp": self._parse_id_list(settings.tcp_excluded_server_ids),
            "load_avg": self._parse_id_list(settings.load_avg_excluded_server_ids),
        }

        async with async_session() as db:
            result = await db.execute(
                select(Server).where(Server.is_active == True)  # noqa: E712
            )
            servers = result.scalars().all()

        for srv in servers:
            if srv.id in excluded_ids:
                continue
            if srv.id not in self._states:
                self._states[srv.id] = ServerAlertState()
            state = self._states[srv.id]

            await self._check_server(srv, state, settings, now)

        monitored_ids = {s.id for s in servers} - excluded_ids
        stale = [k for k in self._states if k not in monitored_ids]
        for k in stale:
            del self._states[k]

    async def _check_server(
        self,
        srv: Server,
        state: ServerAlertState,
        settings: AlertSettings,
        now: float,
    ):
        metrics_json = srv.last_metrics
        metrics = None
        if metrics_json:
            try:
                metrics = json.loads(metrics_json)
            except (json.JSONDecodeError, TypeError):
                pass

        is_online = self._server_is_online(srv, settings)
        tex = self._trigger_excluded

        # --- Offline / Recovery ---
        if settings.offline_enabled and srv.id not in tex["offline"]:
            await self._check_offline(srv, state, settings, is_online, now)

        if not is_online or not metrics:
            return

        # --- Load Average (не зависит от EMA, проверяем сразу) ---
        if settings.load_avg_enabled and srv.id not in tex.get("load_avg", set()):
            await self._check_load_avg(srv, state, settings, now, metrics)

        cpu_val = self._extract_cpu(metrics)
        ram_val = self._extract_ram(metrics)
        raw_rx, raw_tx = self._extract_network(metrics)
        tcp = self._extract_tcp(metrics)

        net_rx_speed, net_tx_speed = self._calc_net_speed(state, raw_rx, raw_tx)

        state.update_ema("ema_cpu", cpu_val)
        state.update_ema("ema_ram", ram_val)
        state.update_ema("ema_net_rx", net_rx_speed)
        state.update_ema("ema_net_tx", net_tx_speed)
        state.update_ema("ema_tcp_established", tcp.get("established", 0))
        state.update_ema("ema_tcp_listen", tcp.get("listen", 0))
        state.update_ema("ema_tcp_timewait", tcp.get("time_wait", 0))
        state.update_ema("ema_tcp_closewait", tcp.get("close_wait", 0))
        state.update_ema("ema_tcp_synsent", tcp.get("syn_sent", 0))
        state.update_ema("ema_tcp_synrecv", tcp.get("syn_recv", 0))
        state.update_ema("ema_tcp_finwait", tcp.get("fin_wait", 0))
        state.samples += 1

        if not state.is_warmed():
            return

        cooldown = settings.alert_cooldown or 1800

        # --- CPU ---
        if settings.cpu_enabled and srv.id not in tex["cpu"]:
            await self._check_resource(
                srv, state, settings, now, cooldown,
                current=cpu_val,
                ema=state.ema_cpu,
                critical_threshold=settings.cpu_critical_threshold,
                spike_percent=settings.cpu_spike_percent,
                sustained=settings.cpu_sustained_seconds,
                critical_type="cpu_critical",
                spike_type="cpu_spike",
                label="CPU",
                unit="%",
                min_value=settings.cpu_min_value or 0,
            )

        # --- RAM ---
        if settings.ram_enabled and srv.id not in tex["ram"]:
            await self._check_resource(
                srv, state, settings, now, cooldown,
                current=ram_val,
                ema=state.ema_ram,
                critical_threshold=settings.ram_critical_threshold,
                spike_percent=settings.ram_spike_percent,
                sustained=settings.ram_sustained_seconds,
                critical_type="ram_critical",
                spike_type="ram_spike",
                label="RAM",
                unit="%",
                min_value=settings.ram_min_value or 0,
            )

        # --- Network ---
        if settings.network_enabled and srv.id not in tex["network"]:
            await self._check_deviation_both(
                srv, state, settings, now, cooldown,
                current_val=net_rx_speed + net_tx_speed,
                ema_val=state.ema_net_rx + state.ema_net_tx,
                spike_pct=settings.network_spike_percent,
                drop_pct=settings.network_drop_percent,
                sustained=settings.network_sustained_seconds,
                spike_type="network_spike",
                drop_type="network_drop",
                label="Network",
                format_fn=self._fmt_bytes_speed,
                min_value=settings.network_min_bytes or 0,
            )

        tcp_min = settings.tcp_min_connections or 0
        tcp_ignored = srv.id in tex["tcp"]

        # --- TCP Established ---
        if settings.tcp_established_enabled and not tcp_ignored:
            await self._check_deviation_both(
                srv, state, settings, now, cooldown,
                current_val=tcp.get("established", 0),
                ema_val=state.ema_tcp_established,
                spike_pct=settings.tcp_established_spike_percent,
                drop_pct=settings.tcp_established_drop_percent,
                sustained=settings.tcp_established_sustained_seconds,
                spike_type="tcp_established_spike",
                drop_type="tcp_established_drop",
                label="TCP Established",
                min_value=tcp_min,
            )

        # --- TCP Listen ---
        if settings.tcp_listen_enabled and not tcp_ignored:
            await self._check_deviation_spike(
                srv, state, settings, now, cooldown,
                current_val=tcp.get("listen", 0),
                ema_val=state.ema_tcp_listen,
                spike_pct=settings.tcp_listen_spike_percent,
                sustained=settings.tcp_listen_sustained_seconds,
                spike_type="tcp_listen_spike",
                label="TCP Listen",
                min_value=tcp_min,
            )

        # --- TCP Time Wait ---
        if settings.tcp_timewait_enabled and not tcp_ignored:
            await self._check_deviation_spike(
                srv, state, settings, now, cooldown,
                current_val=tcp.get("time_wait", 0),
                ema_val=state.ema_tcp_timewait,
                spike_pct=settings.tcp_timewait_spike_percent,
                sustained=settings.tcp_timewait_sustained_seconds,
                spike_type="tcp_timewait_spike",
                label="TCP Time Wait",
                min_value=tcp_min,
            )

        # --- TCP Close Wait ---
        if settings.tcp_closewait_enabled and not tcp_ignored:
            await self._check_deviation_spike(
                srv, state, settings, now, cooldown,
                current_val=tcp.get("close_wait", 0),
                ema_val=state.ema_tcp_closewait,
                spike_pct=settings.tcp_closewait_spike_percent,
                sustained=settings.tcp_closewait_sustained_seconds,
                spike_type="tcp_closewait_spike",
                label="TCP Close Wait",
                min_value=tcp_min,
            )

        # --- TCP SYN Sent ---
        if settings.tcp_synsent_enabled and not tcp_ignored:
            await self._check_deviation_spike(
                srv, state, settings, now, cooldown,
                current_val=tcp.get("syn_sent", 0),
                ema_val=state.ema_tcp_synsent,
                spike_pct=settings.tcp_synsent_spike_percent,
                sustained=settings.tcp_synsent_sustained_seconds,
                spike_type="tcp_synsent_spike",
                label="TCP SYN Sent",
                min_value=tcp_min,
            )

        # --- TCP SYN Recv ---
        if settings.tcp_synrecv_enabled and not tcp_ignored:
            await self._check_deviation_spike(
                srv, state, settings, now, cooldown,
                current_val=tcp.get("syn_recv", 0),
                ema_val=state.ema_tcp_synrecv,
                spike_pct=settings.tcp_synrecv_spike_percent,
                sustained=settings.tcp_synrecv_sustained_seconds,
                spike_type="tcp_synrecv_spike",
                label="TCP SYN Recv",
                min_value=tcp_min,
            )

        # --- TCP FIN Wait ---
        if settings.tcp_finwait_enabled and not tcp_ignored:
            await self._check_deviation_spike(
                srv, state, settings, now, cooldown,
                current_val=tcp.get("fin_wait", 0),
                ema_val=state.ema_tcp_finwait,
                spike_pct=settings.tcp_finwait_spike_percent,
                sustained=settings.tcp_finwait_sustained_seconds,
                spike_type="tcp_finwait_spike",
                label="TCP FIN Wait",
                min_value=tcp_min,
            )

    # ------------------------------------------------------------------
    # Localized message builders
    # ------------------------------------------------------------------
    @staticmethod
    def _lang(settings: AlertSettings) -> str:
        return (settings.language or "en").lower()

    def _msg_offline_full(self, srv: Server, settings: AlertSettings) -> str:
        if self._lang(settings) == "ru":
            return f"Сервер {srv.name} недоступен (API и ICMP не отвечают)"
        return f"Server {srv.name} is offline (API and ICMP unreachable)"

    def _msg_offline_api_only(self, srv: Server, settings: AlertSettings) -> str:
        if self._lang(settings) == "ru":
            return f"Сервер {srv.name} недоступен (API не отвечает, ICMP доступен)"
        return f"Server {srv.name} is offline (API unreachable, ICMP reachable)"

    def _msg_recovery(self, srv: Server, settings: AlertSettings) -> str:
        if self._lang(settings) == "ru":
            return f"Сервер {srv.name} снова онлайн"
        return f"Server {srv.name} is back online"

    def _msg_critical(self, srv: Server, settings: AlertSettings, label: str, current: float, threshold: float, unit: str) -> str:
        if self._lang(settings) == "ru":
            return f"{label} критический на {srv.name}: {current:.1f}{unit} (порог {threshold:.0f}{unit})"
        return f"{label} critical on {srv.name}: {current:.1f}{unit} (threshold {threshold:.0f}{unit})"

    def _msg_spike(self, srv: Server, settings: AlertSettings, label: str, current_str: str, ema_str: str, pct: float) -> str:
        if self._lang(settings) == "ru":
            return f"Скачок {label} на {srv.name}: {current_str} (базовое {ema_str}, +{pct:.0f}%)"
        return f"{label} spike on {srv.name}: {current_str} (baseline {ema_str}, +{pct:.0f}%)"

    def _msg_drop(self, srv: Server, settings: AlertSettings, label: str, current_str: str, ema_str: str, pct: float) -> str:
        if self._lang(settings) == "ru":
            return f"Падение {label} на {srv.name}: {current_str} (базовое {ema_str}, -{pct:.0f}%)"
        return f"{label} drop on {srv.name}: {current_str} (baseline {ema_str}, -{pct:.0f}%)"

    def _msg_header(self, settings: AlertSettings) -> str:
        if self._lang(settings) == "ru":
            return "Уведомление сервера"
        return "Server Alert"

    def _msg_load_avg(self, srv: Server, settings: AlertSettings, load_avg: float, threshold: float, cores: int) -> str:
        if self._lang(settings) == "ru":
            return f"Load Average высокий на {srv.name}: {load_avg:.2f} (порог {threshold:.1f}, ядер: {cores})"
        return f"Load Average high on {srv.name}: {load_avg:.2f} (threshold {threshold:.1f}, cores: {cores})"

    # ------------------------------------------------------------------
    # Load Average
    # ------------------------------------------------------------------
    async def _check_load_avg(
        self,
        srv: Server,
        state: ServerAlertState,
        settings: AlertSettings,
        now: float,
        metrics: dict,
    ):
        cpu = metrics.get("cpu", {})
        cores = cpu.get("cores_logical", 1)
        load_avg_1 = cpu.get("load_avg_1", 0)

        offset = settings.load_avg_threshold_offset if settings.load_avg_threshold_offset is not None else 1.0
        threshold = cores + offset
        required_checks = settings.load_avg_sustained_checks or 3
        cooldown = settings.alert_cooldown or 1800

        if load_avg_1 > threshold:
            state.load_avg_fail_count += 1
            if state.load_avg_fail_count >= required_checks:
                if self._cooldown_ok(state, "load_avg_high", now, cooldown):
                    state.last_alert["load_avg_high"] = now
                    state.load_avg_fail_count = 0
                    await self._send_and_save(
                        srv, settings, "load_avg_high", "warning",
                        self._msg_load_avg(srv, settings, load_avg_1, threshold, cores),
                        {"load_avg_1": round(load_avg_1, 2), "threshold": round(threshold, 2), "cores": cores},
                    )
        else:
            state.load_avg_fail_count = 0

    # ------------------------------------------------------------------
    # Offline / Recovery
    # ------------------------------------------------------------------
    async def _check_offline(
        self,
        srv: Server,
        state: ServerAlertState,
        settings: AlertSettings,
        is_online: bool,
        now: float,
    ):
        cooldown = settings.alert_cooldown or 1800

        if not is_online:
            if not state.was_offline:
                logger.info(f"Server {srv.name} appears offline, starting active probe")
                is_dead, icmp_reachable = await self._active_probe_sequence(srv)

                if not is_dead:
                    logger.info(f"Server {srv.name} responded during probe, not offline")
                    return

                state.was_offline = True

                if self._cooldown_ok(state, "offline", now, cooldown):
                    state.last_alert["offline"] = now

                    if icmp_reachable:
                        message = self._msg_offline_api_only(srv, settings)
                    else:
                        message = self._msg_offline_full(srv, settings)

                    await self._send_and_save(
                        srv, settings, "offline", "critical",
                        message,
                        {"icmp_reachable": icmp_reachable},
                    )
        else:
            if state.was_offline and settings.offline_recovery_notify:
                if self._cooldown_ok(state, "recovery", now, cooldown):
                    state.last_alert["recovery"] = now
                    await self._send_and_save(
                        srv, settings, "recovery", "info",
                        self._msg_recovery(srv, settings),
                        {},
                    )
            state.was_offline = False
            state.alert_start.pop("offline", None)

    # ------------------------------------------------------------------
    # Resource check (CPU / RAM) with critical threshold + spike
    # ------------------------------------------------------------------
    async def _check_resource(
        self,
        srv: Server,
        state: ServerAlertState,
        settings: AlertSettings,
        now: float,
        cooldown: int,
        *,
        current: float,
        ema: float,
        critical_threshold: float,
        spike_percent: float,
        sustained: int,
        critical_type: str,
        spike_type: str,
        label: str,
        unit: str = "%",
        min_value: float = 0,
    ):
        if current >= critical_threshold:
            self._track_condition(state, critical_type, now)
            if self._sustained_met(state, critical_type, now, sustained):
                if self._cooldown_ok(state, critical_type, now, cooldown):
                    state.last_alert[critical_type] = now
                    await self._send_and_save(
                        srv, settings, critical_type, "critical",
                        self._msg_critical(srv, settings, label, current, critical_threshold, unit),
                        {"current": round(current, 1), "threshold": critical_threshold, "ema": round(ema, 1)},
                    )
        else:
            self._clear_condition(state, critical_type)

        if current < min_value:
            self._clear_condition(state, spike_type)
            return

        if ema > 0:
            deviation_pct = ((current - ema) / ema) * 100
        else:
            deviation_pct = 0 if current == 0 else 100

        if deviation_pct > spike_percent:
            self._track_condition(state, spike_type, now)
            if self._sustained_met(state, spike_type, now, sustained):
                if self._cooldown_ok(state, spike_type, now, cooldown):
                    state.last_alert[spike_type] = now
                    await self._send_and_save(
                        srv, settings, spike_type, "warning",
                        self._msg_spike(srv, settings, label, f"{current:.1f}{unit}", f"{ema:.1f}{unit}", deviation_pct),
                        {"current": round(current, 1), "ema": round(ema, 1), "deviation_pct": round(deviation_pct, 1)},
                    )
        else:
            self._clear_condition(state, spike_type)

    # ------------------------------------------------------------------
    # Deviation check (spike + drop) for network / tcp
    # ------------------------------------------------------------------
    async def _check_deviation_both(
        self,
        srv: Server,
        state: ServerAlertState,
        settings: AlertSettings,
        now: float,
        cooldown: int,
        *,
        current_val: float,
        ema_val: float,
        spike_pct: float,
        drop_pct: float,
        sustained: int,
        spike_type: str,
        drop_type: str,
        label: str,
        format_fn=None,
        min_value: float = 0,
    ):
        # Гейт шума: если текущая активность ниже порога значимости —
        # ни падение, ни всплеск не считаем аномалией. Падение с высокого
        # базового до idle (например, ночной простой) — это нормальная
        # смена нагрузки, а не инцидент.
        if current_val < min_value:
            self._clear_condition(state, spike_type)
            self._clear_condition(state, drop_type)
            return

        fmt = format_fn or (lambda v: f"{v:.0f}")

        if ema_val > 0:
            increase_pct = ((current_val - ema_val) / ema_val) * 100
        else:
            increase_pct = 0 if current_val == 0 else 100

        if increase_pct > spike_pct:
            self._track_condition(state, spike_type, now)
            if self._sustained_met(state, spike_type, now, sustained):
                if self._cooldown_ok(state, spike_type, now, cooldown):
                    state.last_alert[spike_type] = now
                    await self._send_and_save(
                        srv, settings, spike_type, "warning",
                        self._msg_spike(srv, settings, label, fmt(current_val), fmt(ema_val), increase_pct),
                        {"current": current_val, "ema": ema_val, "deviation_pct": round(increase_pct, 1)},
                    )
        else:
            self._clear_condition(state, spike_type)

        if ema_val > 0:
            decrease_pct = ((ema_val - current_val) / ema_val) * 100
            if decrease_pct > drop_pct:
                self._track_condition(state, drop_type, now)
                if self._sustained_met(state, drop_type, now, sustained):
                    if self._cooldown_ok(state, drop_type, now, cooldown):
                        state.last_alert[drop_type] = now
                        await self._send_and_save(
                            srv, settings, drop_type, "warning",
                            self._msg_drop(srv, settings, label, fmt(current_val), fmt(ema_val), decrease_pct),
                            {"current": current_val, "ema": ema_val, "deviation_pct": round(decrease_pct, 1)},
                        )
            else:
                self._clear_condition(state, drop_type)
        else:
            self._clear_condition(state, drop_type)

    async def _check_deviation_spike(
        self,
        srv: Server,
        state: ServerAlertState,
        settings: AlertSettings,
        now: float,
        cooldown: int,
        *,
        current_val: float,
        ema_val: float,
        spike_pct: float,
        sustained: int,
        spike_type: str,
        label: str,
        min_value: float = 0,
    ):
        if current_val < min_value:
            self._clear_condition(state, spike_type)
            return

        if ema_val > 0:
            increase_pct = ((current_val - ema_val) / ema_val) * 100
        else:
            increase_pct = 0 if current_val == 0 else 100

        if increase_pct > spike_pct:
            self._track_condition(state, spike_type, now)
            if self._sustained_met(state, spike_type, now, sustained):
                if self._cooldown_ok(state, spike_type, now, cooldown):
                    state.last_alert[spike_type] = now
                    await self._send_and_save(
                        srv, settings, spike_type, "warning",
                        self._msg_spike(srv, settings, label, f"{current_val:.0f}", f"{ema_val:.0f}", increase_pct),
                        {"current": current_val, "ema": ema_val, "deviation_pct": round(increase_pct, 1)},
                    )
        else:
            self._clear_condition(state, spike_type)

    # ------------------------------------------------------------------
    # Helpers: sustained tracking, cooldown, condition management
    # ------------------------------------------------------------------
    @staticmethod
    def _track_condition(state: ServerAlertState, alert_type: str, now: float):
        if alert_type not in state.alert_start:
            state.alert_start[alert_type] = now

    @staticmethod
    def _clear_condition(state: ServerAlertState, alert_type: str):
        state.alert_start.pop(alert_type, None)

    @staticmethod
    def _sustained_met(state: ServerAlertState, alert_type: str, now: float, sustained: int) -> bool:
        started = state.alert_start.get(alert_type)
        if started is None:
            return False
        return (now - started) >= sustained

    @staticmethod
    def _cooldown_ok(state: ServerAlertState, alert_type: str, now: float, cooldown: int) -> bool:
        last = state.last_alert.get(alert_type)
        if last is None:
            return True
        return (now - last) >= cooldown

    # ------------------------------------------------------------------
    # Metric extraction from cached JSON
    # ------------------------------------------------------------------
    @staticmethod
    def _server_is_online(srv: Server, settings: AlertSettings) -> bool:
        if not srv.last_seen:
            return False
        try:
            last_seen = srv.last_seen
            if last_seen.tzinfo is not None:
                last_seen = last_seen.replace(tzinfo=None)
            now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
            interval = settings.check_interval or 60
            threshold = max(1, settings.offline_fail_threshold or 3)
            max_gap = timedelta(seconds=interval * threshold + 30)
            return (now_utc - last_seen) < max_gap
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Active probe methods
    # ------------------------------------------------------------------
    @staticmethod
    async def _api_probe(srv: Server) -> bool:
        try:
            client = get_node_client(srv)
            response = await client.get(
                f"{srv.url}/api/metrics",
                headers=node_auth_headers(srv),
                timeout=5.0,
            )
            return response.status_code == 200
        except Exception:
            return False

    @staticmethod
    async def _icmp_ping(srv: Server) -> bool:
        try:
            hostname = urlparse(srv.url).hostname
            if not hostname:
                return False
            proc = await asyncio.create_subprocess_exec(
                "ping", "-c", "1", "-W", "3", hostname,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            return await proc.wait() == 0
        except Exception:
            return False

    async def _active_probe_sequence(self, srv: Server) -> tuple[bool, bool | None]:
        """Возвращает (is_dead, icmp_reachable). icmp_reachable=None если сервер ожил во время пробинга."""
        for attempt in range(3):
            if await self._api_probe(srv):
                return False, None
            if attempt < 2:
                await asyncio.sleep(1)

        icmp_ok = await self._icmp_ping(srv)

        if not icmp_ok:
            return True, False

        # ICMP доступен, пробуем API ещё 2 раза с паузой 5 сек
        for attempt in range(2):
            await asyncio.sleep(5)
            if await self._api_probe(srv):
                return False, None

        return True, True

    # ------------------------------------------------------------------
    # Metric extraction from cached JSON
    # ------------------------------------------------------------------
    @staticmethod
    def _extract_cpu(metrics: dict) -> float:
        cpu = metrics.get("cpu", {})
        return cpu.get("usage_percent", 0) or 0

    @staticmethod
    def _extract_ram(metrics: dict) -> float:
        mem = metrics.get("memory", {})
        ram = mem.get("ram", {})
        return ram.get("percent", 0) or 0

    @staticmethod
    def _extract_network(metrics: dict) -> tuple[float, float]:
        """Extract raw cumulative byte counters (NOT speed)."""
        net = metrics.get("network", {})
        total = net.get("total", {})
        return total.get("rx_bytes", 0) or 0, total.get("tx_bytes", 0) or 0

    @staticmethod
    def _calc_net_speed(state: "ServerAlertState", raw_rx: float, raw_tx: float) -> tuple[float, float]:
        """Calculate bytes/sec from consecutive cumulative counter readings."""
        current_time = time.time()
        rx_speed = 0.0
        tx_speed = 0.0

        if state.net_initialized and state.prev_time > 0:
            dt = current_time - state.prev_time
            if dt > 0.5:
                rx_diff = raw_rx - state.prev_net_rx
                tx_diff = raw_tx - state.prev_net_tx
                if rx_diff >= 0:
                    rx_speed = rx_diff / dt
                if tx_diff >= 0:
                    tx_speed = tx_diff / dt

        state.prev_net_rx = raw_rx
        state.prev_net_tx = raw_tx
        state.prev_time = current_time
        state.net_initialized = True

        return rx_speed, tx_speed

    @staticmethod
    def _extract_tcp(metrics: dict) -> dict:
        system = metrics.get("system", {})
        conn = system.get("connections", {})
        detailed = system.get("connections_detailed", {}).get("tcp", {})
        src = detailed if detailed else conn
        return {
            "established": (src.get("established", 0) or 0),
            "listen": (src.get("listen", 0) or 0),
            "time_wait": (src.get("time_wait", 0) or 0),
            "close_wait": (src.get("close_wait", 0) or 0),
            "syn_sent": (src.get("syn_sent", 0) or 0),
            "syn_recv": (src.get("syn_recv", 0) or src.get("syn_received", 0) or 0),
            "fin_wait": (src.get("fin_wait", 0) or 0),
        }

    @staticmethod
    def _fmt_bytes_speed(val: float) -> str:
        if val >= 1_000_000_000:
            return f"{val / 1_000_000_000:.1f} GB/s"
        if val >= 1_000_000:
            return f"{val / 1_000_000:.1f} MB/s"
        if val >= 1_000:
            return f"{val / 1_000:.1f} KB/s"
        return f"{val:.0f} B/s"

    # ------------------------------------------------------------------
    # Save to DB + send Telegram
    # ------------------------------------------------------------------
    async def _send_and_save(
        self,
        srv: Server,
        settings: AlertSettings,
        alert_type: str,
        severity: str,
        message: str,
        details: dict,
    ):
        notified = False
        if settings.telegram_bot_token and settings.telegram_chat_id:
            severity_map = {"critical": "\U0001f534", "warning": "\U0001f7e1", "info": "\U0001f7e2"}
            emoji = severity_map.get(severity, "\u2139\ufe0f")
            header = self._msg_header(settings)
            text = f"{emoji} <b>{header}</b>\n\n{message}"
            from app.services.telegram_bot import get_telegram_bot_service
            notified = await get_telegram_bot_service().send_message(
                settings.telegram_bot_token, settings.telegram_chat_id, text,
            )

        try:
            async with async_session() as db:
                entry = AlertHistory(
                    server_id=srv.id,
                    server_name=srv.name,
                    alert_type=alert_type,
                    severity=severity,
                    message=message,
                    details=json.dumps(details, ensure_ascii=False),
                    notified=notified,
                )
                db.add(entry)
                await db.commit()
        except Exception as e:
            logger.error(f"Failed to save alert history: {e}")

    async def test_telegram(self, bot_token: str, chat_id: str) -> dict:
        from app.services.telegram_bot import get_telegram_bot_service
        text = "\u2705 <b>Test alert</b>\n\nServer monitoring alerts configured successfully!"
        return await get_telegram_bot_service().send_test(bot_token, chat_id, text)


# Singleton
_alerter: Optional[ServerAlerter] = None


def get_server_alerter() -> ServerAlerter:
    global _alerter
    if _alerter is None:
        _alerter = ServerAlerter()
    return _alerter


async def start_server_alerter():
    alerter = get_server_alerter()
    await alerter.start()


async def stop_server_alerter():
    alerter = get_server_alerter()
    await alerter.stop()
