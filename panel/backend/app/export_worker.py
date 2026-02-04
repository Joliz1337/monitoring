"""Standalone export worker script.

Runs as a separate process to avoid blocking the main FastAPI event loop.
"""

import sys
import os
import json
from datetime import datetime, timedelta, timezone

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine, select, func as sql_func
from sqlalchemy.orm import sessionmaker

from app.models import (
    XrayVisitStats, RemnawaveUserCache, XrayUserIpStats, RemnawaveExport
)
from app.config import settings as app_settings


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
    # Create sync engine and session
    engine = create_engine(
        app_settings.DATABASE_URL.replace("+asyncpg", "").replace("postgresql://", "postgresql+psycopg2://")
    )
    Session = sessionmaker(bind=engine)
    
    with Session() as db:
        try:
            # Update status to processing
            export_record = db.get(RemnawaveExport, export_id)
            if not export_record:
                print(f"Export record {export_id} not found")
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
                if settings["period"] == "all":
                    main_query = select(
                        XrayVisitStats.email,
                        XrayVisitStats.destination,
                        sql_func.sum(XrayVisitStats.visit_count).label('visits'),
                        sql_func.min(XrayVisitStats.first_seen).label('first_seen'),
                        sql_func.max(XrayVisitStats.last_seen).label('last_seen')
                    ).group_by(
                        XrayVisitStats.email, 
                        XrayVisitStats.destination
                    ).order_by(XrayVisitStats.email)
                else:
                    main_query = select(
                        XrayVisitStats.email,
                        XrayVisitStats.destination,
                        sql_func.sum(XrayVisitStats.visit_count).label('visits'),
                        sql_func.min(XrayVisitStats.first_seen).label('first_seen'),
                        sql_func.max(XrayVisitStats.last_seen).label('last_seen')
                    ).where(
                        XrayVisitStats.last_seen >= start_time
                    ).group_by(
                        XrayVisitStats.email,
                        XrayVisitStats.destination
                    ).order_by(XrayVisitStats.email)
            else:
                if settings["period"] == "all":
                    main_query = select(
                        XrayVisitStats.email,
                        sql_func.sum(XrayVisitStats.visit_count).label('total_visits'),
                        sql_func.count(sql_func.distinct(XrayVisitStats.destination)).label('unique_sites')
                    ).group_by(XrayVisitStats.email)
                else:
                    main_query = select(
                        XrayVisitStats.email,
                        sql_func.sum(XrayVisitStats.visit_count).label('total_visits'),
                        sql_func.count(sql_func.distinct(XrayVisitStats.destination)).label('unique_sites')
                    ).where(
                        XrayVisitStats.last_seen >= start_time
                    ).group_by(XrayVisitStats.email)
            
            main_result = db.execute(main_query)
            main_data = main_result.all()
            
            # Get IPs if needed
            user_ips = {}
            if settings.get("include_client_ips") or settings.get("include_infra_ips"):
                ip_query = select(
                    XrayUserIpStats.email,
                    XrayUserIpStats.source_ip,
                    XrayUserIpStats.is_infrastructure
                ).group_by(
                    XrayUserIpStats.email,
                    XrayUserIpStats.source_ip,
                    XrayUserIpStats.is_infrastructure
                )
                ip_result = db.execute(ip_query)
                
                for ip_row in ip_result.all():
                    if ip_row.email not in user_ips:
                        user_ips[ip_row.email] = {"client": [], "infra": []}
                    if ip_row.is_infrastructure:
                        user_ips[ip_row.email]["infra"].append(ip_row.source_ip)
                    else:
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
            
            print(f"Export {export_id} completed: {len(export_rows)} rows, {file_size} bytes")
            
        except Exception as e:
            print(f"Export {export_id} failed: {e}")
            export_record = db.get(RemnawaveExport, export_id)
            if export_record:
                export_record.status = "failed"
                export_record.error_message = str(e)
                db.commit()
    
    engine.dispose()


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Usage: python export_worker.py <export_id> <settings_json>")
        sys.exit(1)
    
    export_id = int(sys.argv[1])
    settings = json.loads(sys.argv[2])
    
    run_export(export_id, settings)
