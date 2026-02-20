"""Standalone export worker script.

Runs as a separate process to avoid blocking the main FastAPI event loop.
"""

import sys
import os
import json
import logging
from datetime import datetime, timedelta, timezone

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Setup logging to file
log_dir = os.path.join(os.path.dirname(__file__), "exports")
os.makedirs(log_dir, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(os.path.join(log_dir, "export_worker.log")),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

try:
    from sqlalchemy import create_engine, select, func as sql_func
    from sqlalchemy.orm import sessionmaker
    from app.models import (
        XrayStats, RemnawaveUserCache, RemnawaveExport
    )
    from app.config import get_settings
    logger.info("Imports successful")
except Exception as e:
    logger.error(f"Import error: {e}")
    raise


def get_time_filter(period: str):
    """Get start time based on period."""
    if period == "all":
        return None
    
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    periods = {
        "1h": timedelta(hours=1),
        "24h": timedelta(hours=24),
        "7d": timedelta(days=7),
        "30d": timedelta(days=30),
        "365d": timedelta(days=365)
    }
    delta = periods.get(period)
    return now - delta if delta else None


def generate_xlsx(file_path: str, export_rows: list):
    """Generate XLSX file."""
    from openpyxl import Workbook
    
    text_fields = {"telegram_id", "traffic_used_bytes", "traffic_limit_bytes"}
    
    wb = Workbook(write_only=True)
    ws = wb.create_sheet("Export")
    
    if export_rows:
        headers = list(export_rows[0].keys())
        ws.append(headers)
        
        for row_data in export_rows:
            row_values = []
            for header in headers:
                value = row_data.get(header)
                if isinstance(value, list):
                    value = "; ".join(str(v) for v in value)
                elif header in text_fields and value is not None:
                    value = str(value)
                row_values.append(value)
            ws.append(row_values)
    
    wb.save(file_path)


def run_export(export_id: int, settings: dict):
    """Run export task synchronously."""
    logger.info(f"Starting export {export_id} with settings: {settings}")
    
    # Create sync engine and session using sync URL
    app_settings = get_settings()
    db_url = app_settings.sync_database_url
    logger.info(f"Using sync DATABASE_URL: {db_url}")
    
    engine = create_engine(db_url)
    Session = sessionmaker(bind=engine)
    
    with Session() as db:
        try:
            # Update status to processing
            export_record = db.get(RemnawaveExport, export_id)
            if not export_record:
                logger.error(f"Export record {export_id} not found")
                return
            
            export_record.status = "processing"
            db.commit()
            
            start_time = get_time_filter(settings["period"])
            
            # Get user cache
            cache_result = db.execute(select(RemnawaveUserCache))
            user_cache = {u.email: u for u in cache_result.scalars().all()}
            
            # Build main query based on settings
            include_destinations = settings.get("include_destinations", True)
            
            if include_destinations:
                base_query = select(
                    XrayStats.email,
                    XrayStats.host.label('destination'),
                    sql_func.sum(XrayStats.count).label('visits'),
                    sql_func.min(XrayStats.first_seen).label('first_seen'),
                    sql_func.max(XrayStats.last_seen).label('last_seen')
                )
                if settings["period"] != "all" and start_time:
                    base_query = base_query.where(XrayStats.last_seen >= start_time)
                main_query = base_query.group_by(XrayStats.email, XrayStats.host).order_by(XrayStats.email)
            else:
                base_query = select(
                    XrayStats.email,
                    sql_func.sum(XrayStats.count).label('total_visits'),
                    sql_func.count(sql_func.distinct(XrayStats.host)).label('unique_sites')
                )
                if settings["period"] != "all" and start_time:
                    base_query = base_query.where(XrayStats.last_seen >= start_time)
                main_query = base_query.group_by(XrayStats.email)
            
            main_result = db.execute(main_query)
            main_data = main_result.all()
            
            # Get IPs if needed (directly from xray_stats, no JOIN needed)
            user_ips = {}
            if settings.get("include_client_ips") or settings.get("include_infra_ips"):
                ip_query = select(XrayStats.email, XrayStats.source_ip).group_by(XrayStats.email, XrayStats.source_ip)
                ip_result = db.execute(ip_query)
                
                for ip_row in ip_result.all():
                    if ip_row.email not in user_ips:
                        user_ips[ip_row.email] = {"client": [], "infra": []}
                    # All IPs go to client (infrastructure detection not stored in DB anymore)
                    user_ips[ip_row.email]["client"].append(ip_row.source_ip)
            
            # Build export rows
            export_rows = []
            
            for row in main_data:
                user_info = user_cache.get(row.email)
                ips = user_ips.get(row.email, {"client": [], "infra": []})
                
                row_data = {}
                
                # User fields
                if settings.get("include_user_id", True):
                    row_data["user_id"] = row.email
                if settings.get("include_username", True):
                    row_data["username"] = (user_info.username if user_info else None) or f"User #{row.email}"
                if settings.get("include_status", True):
                    row_data["status"] = (user_info.status if user_info else None) or "UNKNOWN"
                
                # Telegram ID
                if settings.get("include_telegram_id", False):
                    tg_id = user_info.telegram_id if user_info else None
                    row_data["telegram_id"] = tg_id if tg_id else ""
                
                # Destination fields
                if include_destinations:
                    row_data["destination"] = row.destination
                    if settings.get("include_visits_count", True):
                        row_data["visits"] = row.visits
                    if settings.get("include_first_seen", True):
                        row_data["first_seen"] = row.first_seen.isoformat() if row.first_seen else None
                    if settings.get("include_last_seen", True):
                        row_data["last_seen"] = row.last_seen.isoformat() if row.last_seen else None
                else:
                    if settings.get("include_visits_count", True):
                        row_data["total_visits"] = row.total_visits
                        row_data["unique_sites"] = row.unique_sites
                
                # IP fields
                if settings.get("include_client_ips", False):
                    row_data["client_ips"] = ips["client"]
                if settings.get("include_infra_ips", False):
                    row_data["infrastructure_ips"] = ips["infra"]
                
                # Traffic fields
                if settings.get("include_traffic", False):
                    row_data["traffic_used_bytes"] = user_info.used_traffic_bytes if user_info else None
                    row_data["traffic_limit_bytes"] = user_info.traffic_limit_bytes if user_info else None
                
                export_rows.append(row_data)
            
            # Generate file
            exports_dir = os.path.join(os.path.dirname(__file__), "exports")
            os.makedirs(exports_dir, exist_ok=True)
            file_path = os.path.join(exports_dir, export_record.filename)
            
            generate_xlsx(file_path, export_rows)
            
            # Update export record
            file_size = os.path.getsize(file_path)
            export_record.status = "completed"
            export_record.file_size = file_size
            export_record.rows_count = len(export_rows)
            export_record.completed_at = datetime.now(timezone.utc).replace(tzinfo=None)
            db.commit()
            
            logger.info(f"Export {export_id} completed: {len(export_rows)} rows, {file_size} bytes")
            
        except Exception as e:
            logger.exception(f"Export {export_id} failed: {e}")
            export_record = db.get(RemnawaveExport, export_id)
            if export_record:
                export_record.status = "failed"
                export_record.error_message = str(e)
                db.commit()
    
    engine.dispose()


if __name__ == "__main__":
    logger.info(f"Export worker started with args: {sys.argv}")
    
    if len(sys.argv) != 3:
        logger.error("Usage: python export_worker.py <export_id> <settings_json>")
        sys.exit(1)
    
    try:
        export_id = int(sys.argv[1])
        settings = json.loads(sys.argv[2])
        logger.info(f"Parsed export_id={export_id}")
        run_export(export_id, settings)
    except Exception as e:
        logger.exception(f"Export worker failed: {e}")
        sys.exit(1)
