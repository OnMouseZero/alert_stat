import datetime
import logging
import os
import sqlite3
from logging.handlers import RotatingFileHandler
from pathlib import Path

from flask import Flask, redirect, render_template, request, url_for


BASE_DIR = Path(__file__).resolve().parent
DB_FILE = str(Path(os.getenv("ALERT_DB_PATH", str(BASE_DIR / "alerts.db"))))
PORT = int(os.getenv("ALERT_DASHBOARD_PORT", "5002"))
RECOVERY_STATS_START_TEXT = os.getenv("ALERT_RECOVERY_STATS_START", "2026-06-01")
LOG_DIR = Path(os.getenv("ALERT_DASHBOARD_LOG_DIR", str(BASE_DIR / "logs")))
LOG_FILE = Path(os.getenv("ALERT_DASHBOARD_LOG_FILE", str(LOG_DIR / "alert_dashboard.log")))
LOG_LEVEL = os.getenv("ALERT_DASHBOARD_LOG_LEVEL", "INFO").upper()
LOG_MAX_BYTES = int(os.getenv("ALERT_DASHBOARD_LOG_MAX_BYTES", str(10 * 1024 * 1024)))
LOG_BACKUP_COUNT = int(os.getenv("ALERT_DASHBOARD_LOG_BACKUP_COUNT", "5"))


def setup_logging():
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

    formatter = logging.Formatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s")
    log_level = getattr(logging, LOG_LEVEL, logging.INFO)

    root_logger = logging.getLogger()
    root_logger.setLevel(log_level)
    root_logger.handlers.clear()

    stream_handler = logging.StreamHandler()
    stream_handler.setLevel(log_level)
    stream_handler.setFormatter(formatter)

    file_handler = RotatingFileHandler(
        LOG_FILE,
        maxBytes=LOG_MAX_BYTES,
        backupCount=LOG_BACKUP_COUNT,
        encoding="utf-8",
    )
    file_handler.setLevel(log_level)
    file_handler.setFormatter(formatter)

    root_logger.addHandler(stream_handler)
    root_logger.addHandler(file_handler)


setup_logging()
logger = logging.getLogger(__name__)

app = Flask(__name__)


def get_db_connection():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn


def column_exists(cursor, table_name, column_name):
    cursor.execute(f"PRAGMA table_info({table_name})")
    return any(row[1] == column_name for row in cursor.fetchall())


def ensure_column(cursor, table_name, column_name, definition):
    if not column_exists(cursor, table_name, column_name):
        cursor.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {definition}")


def init_db():
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        ensure_column(cursor, "weekly_alerts", "remark", "TEXT")
        ensure_column(cursor, "weekly_alerts", "remark_updated_at", "TEXT")
        conn.commit()
    finally:
        conn.close()


def parse_dashboard_date(raw_value):
    if not raw_value:
        return datetime.date.today()

    try:
        return datetime.datetime.strptime(raw_value, "%Y-%m-%d").date()
    except ValueError:
        return datetime.date.today()


def make_datetime_range(target_date):
    start_dt = datetime.datetime.combine(target_date, datetime.time.min)
    end_dt = datetime.datetime.combine(target_date, datetime.time.max.replace(microsecond=0))
    return start_dt.strftime("%Y-%m-%d %H:%M:%S"), end_dt.strftime("%Y-%m-%d %H:%M:%S")


def make_recovery_stats_start():
    return f"{RECOVERY_STATS_START_TEXT} 00:00:00"


def compute_duration_text(start_text, end_text):
    if not start_text:
        return "-"

    try:
        start_dt = datetime.datetime.strptime(start_text, "%Y-%m-%d %H:%M:%S")
        end_dt = datetime.datetime.strptime(end_text, "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return "-"

    total_seconds = max(int((end_dt - start_dt).total_seconds()), 0)
    days, remainder = divmod(total_seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, _ = divmod(remainder, 60)

    parts = []
    if days:
        parts.append(f"{days}天")
    if hours:
        parts.append(f"{hours}小时")
    if minutes or not parts:
        parts.append(f"{minutes}分钟")
    return "".join(parts)


def serialize_alert_rows(rows, now_text, recovery_stats_start_text):
    records = []
    for row in rows:
        record = dict(row)
        current_status = record.get("status") or "firing"
        created_at = record.get("created_at") or ""
        record["recovery_in_scope"] = created_at >= recovery_stats_start_text

        if record["recovery_in_scope"]:
            is_resolved = current_status == "resolved"
            end_text = record.get("resolved_at") or now_text
            record["status_label"] = "已恢复" if is_resolved else "未恢复"
            record["status_css"] = "resolved" if is_resolved else "firing"
            record["duration_text"] = compute_duration_text(record.get("starts_at"), end_text)
        else:
            record["status_label"] = "不纳统计"
            record["status_css"] = "ignored"
            record["duration_text"] = "-"

        record["level_label"] = f"Lv.{record.get('level') or '-'}"
        records.append(record)
    return records


def fetch_dashboard_data(target_date):
    start_text, end_text = make_datetime_range(target_date)
    now_text = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    recovery_stats_start = make_recovery_stats_start()

    summary_sql = """
    SELECT
        COUNT(*) AS total_count,
        SUM(CASE WHEN created_at >= ? AND COALESCE(status, 'firing') = 'resolved' THEN 1 ELSE 0 END) AS resolved_count,
        SUM(CASE WHEN created_at >= ? AND COALESCE(status, 'firing') != 'resolved' THEN 1 ELSE 0 END) AS unresolved_count,
        SUM(CASE WHEN level = '4' THEN 1 ELSE 0 END) AS level4_count,
        SUM(CASE WHEN level = '3' THEN 1 ELSE 0 END) AS level3_count,
        SUM(CASE WHEN level = '2' THEN 1 ELSE 0 END) AS level2_count,
        SUM(CASE WHEN level = '1' THEN 1 ELSE 0 END) AS level1_count
    FROM weekly_alerts
    WHERE created_at BETWEEN ? AND ?
    """

    selected_day_alerts_sql = """
    SELECT
        id, alert_name, cluster, namespace, level, metric_type, target,
        key_info, detail_info, first_status, status, starts_at, ends_at,
        created_at, updated_at, resolved_at, remark, remark_updated_at
    FROM weekly_alerts
    WHERE created_at BETWEEN ? AND ?
    ORDER BY
        CASE WHEN COALESCE(status, 'firing') != 'resolved' THEN 0 ELSE 1 END,
        CAST(COALESCE(level, '0') AS INTEGER) DESC,
        created_at DESC
    LIMIT 200
    """

    historical_unresolved_sql = """
    SELECT
        id, alert_name, cluster, namespace, level, metric_type, target,
        key_info, detail_info, first_status, status, starts_at, ends_at,
        created_at, updated_at, resolved_at, remark, remark_updated_at
    FROM weekly_alerts
    WHERE created_at < ?
      AND created_at >= ?
      AND COALESCE(first_status, 'firing') = 'firing'
      AND COALESCE(status, 'firing') != 'resolved'
    ORDER BY CAST(COALESCE(level, '0') AS INTEGER) DESC, created_at ASC
    LIMIT 200
    """

    historical_unresolved_count_sql = """
    SELECT COUNT(*) AS total_count
    FROM weekly_alerts
    WHERE created_at < ?
      AND created_at >= ?
      AND COALESCE(first_status, 'firing') = 'firing'
      AND COALESCE(status, 'firing') != 'resolved'
    """

    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(summary_sql, (recovery_stats_start, recovery_stats_start, start_text, end_text))
        summary_row = cursor.fetchone()

        cursor.execute(selected_day_alerts_sql, (start_text, end_text))
        selected_day_rows = cursor.fetchall()

        cursor.execute(historical_unresolved_sql, (start_text, recovery_stats_start))
        historical_rows = cursor.fetchall()

        cursor.execute(historical_unresolved_count_sql, (start_text, recovery_stats_start))
        historical_unresolved_count_row = cursor.fetchone()
    finally:
        conn.close()

    summary = {
        "total_count": summary_row["total_count"] or 0,
        "resolved_count": summary_row["resolved_count"] or 0,
        "unresolved_count": summary_row["unresolved_count"] or 0,
        "historical_unresolved_count": historical_unresolved_count_row["total_count"] or 0,
        "level4_count": summary_row["level4_count"] or 0,
        "level3_count": summary_row["level3_count"] or 0,
        "level2_count": summary_row["level2_count"] or 0,
        "level1_count": summary_row["level1_count"] or 0,
    }

    return {
        "summary": summary,
        "selected_day_alerts": serialize_alert_rows(selected_day_rows, now_text, recovery_stats_start),
        "historical_unresolved_alerts": serialize_alert_rows(historical_rows, now_text, recovery_stats_start),
        "selected_date_text": target_date.strftime("%Y-%m-%d"),
        "selected_day_label": target_date.strftime("%Y年%m月%d日"),
        "today_text": datetime.date.today().strftime("%Y-%m-%d"),
        "generated_at": now_text,
        "recovery_stats_start_text": RECOVERY_STATS_START_TEXT,
    }


@app.route("/health", methods=["GET"])
def health_check():
    return "dashboard alive", 200


@app.route("/", methods=["GET"])
@app.route("/dashboard", methods=["GET"])
def dashboard():
    target_date = parse_dashboard_date(request.args.get("date", ""))
    logger.info("打开告警看板: date=%s", target_date.strftime("%Y-%m-%d"))
    return render_template("dashboard.html", **fetch_dashboard_data(target_date))


@app.route("/remarks", methods=["POST"])
def save_remark():
    alert_id = request.form.get("alert_id", "").strip()
    remark = request.form.get("remark", "").strip()
    selected_date = request.form.get("date", "").strip()

    if not alert_id.isdigit():
        logger.warning("备注保存失败: 非法 alert_id=%s", alert_id)
        return redirect(url_for("dashboard", date=selected_date or datetime.date.today().strftime("%Y-%m-%d")))

    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            """
            UPDATE weekly_alerts
            SET remark = ?, remark_updated_at = ?
            WHERE id = ?
            """,
            (remark, datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), int(alert_id)),
        )
        conn.commit()
    finally:
        conn.close()

    logger.info("备注保存成功: alert_id=%s", alert_id)
    return redirect(url_for("dashboard", date=selected_date or datetime.date.today().strftime("%Y-%m-%d")))


if __name__ == "__main__":
    init_db()
    logger.info("🚀 告警看板服务启动，监听端口: %s, db=%s", PORT, DB_FILE)
    app.run(host="0.0.0.0", port=PORT)
