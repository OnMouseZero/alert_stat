import datetime
import hashlib
import json
import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path

import pymysql
from flask import Flask, jsonify, request

from db_utils import get_db_connection, get_db_display_name, init_db_schema


BASE_DIR = Path(__file__).resolve().parent
PORT = int(os.getenv("ALERT_WEBHOOK_PORT", "5001"))
LOG_DIR = Path(os.getenv("ALERT_LOG_DIR", str(BASE_DIR / "logs")))
LOG_FILE = Path(os.getenv("ALERT_LOG_FILE", str(LOG_DIR / "web_server_new.log")))
LOG_LEVEL = os.getenv("ALERT_LOG_LEVEL", "INFO").upper()
LOG_MAX_BYTES = int(os.getenv("ALERT_LOG_MAX_BYTES", str(10 * 1024 * 1024)))
LOG_BACKUP_COUNT = int(os.getenv("ALERT_LOG_BACKUP_COUNT", "5"))


def setup_logging():
    """同时输出到 stdout 和滚动日志文件，方便 systemd/journalctl 与落盘排障。"""
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


def parse_alert_time(raw_value):
    """将 Alertmanager 的 ISO 时间尽量转为本地可读格式。"""
    if not raw_value:
        return None

    normalized = raw_value.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"

    try:
        dt = datetime.datetime.fromisoformat(normalized)
        if dt.tzinfo:
            dt = dt.astimezone()
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        pass

    try:
        dt = datetime.datetime.strptime(raw_value.split(".")[0], "%Y-%m-%dT%H:%M:%S")
        dt = dt + datetime.timedelta(hours=8)
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def build_fingerprint(alert, labels, alert_name, cluster, namespace, target):
    """优先使用 Alertmanager fingerprint，没有则用关键标签生成稳定指纹。"""
    fingerprint = alert.get("fingerprint")
    if fingerprint:
        return str(fingerprint)

    stable_payload = {
        "alert_name": alert_name,
        "cluster": cluster,
        "namespace": namespace,
        "target": target,
        "labels": labels,
    }
    serialized = json.dumps(stable_payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha1(serialized.encode("utf-8")).hexdigest()


def init_db():
    init_db_schema(logger)


@app.route("/health", methods=["GET"])
def health_check():
    return "I am alive!", 200


@app.route("/webhook", methods=["POST"])
def webhook():
    """接收 Alertmanager 发来的 JSON 并映射到数据库字段。"""
    logger.info("====== [Webhook 收到新请求] ======")
    logger.info("请求源 IP: %s | 请求头: %s", request.remote_addr, dict(request.headers))

    try:
        data = request.get_json(silent=True)
        if not data:
            logger.warning("⚠️ 收到空请求，或者 Content-Type 不是 application/json")
            return "No JSON data received", 400

        alerts = data.get("alerts", [])
        logger.info("📊 成功解析 JSON 负载。包含的告警条数: %s", len(alerts))

        stored_count = 0
        updated_count = 0

        conn = get_db_connection()
        try:
            with conn.cursor() as cursor:
                for index, alert in enumerate(alerts):
                    event_status = str(alert.get("status", "firing")).lower()
                    labels = alert.get("labels", {})
                    annotations = alert.get("annotations", {})

                    alert_name = labels.get("metricName", labels.get("alertname", "Unknown Alert"))
                    cluster = labels.get("clusterName", labels.get("cluster", "default"))
                    level = labels.get("alertLevel", "0")
                    target = labels.get("alertTarget", labels.get("instance", "Unknown"))

                    logger.info(
                        "-> 正在处理第 %s 条: [%s] %s | 状态: %s | 级别: %s | 对象: %s",
                        index + 1,
                        cluster,
                        alert_name,
                        event_status,
                        level,
                        target,
                    )

                    if event_status not in {"firing", "resolved"}:
                        logger.warning("⚠️ 忽略未知状态告警: %s", event_status)
                        continue

                    namespace = labels.get("namespace", "-")
                    metric_type = labels.get("metricType", "通用")
                    raw_desc = annotations.get("description", "")
                    key_info = annotations.get("alertPoint", raw_desc[:50] + "..." if len(raw_desc) > 50 else raw_desc)
                    detail_info = annotations.get("alertContent", annotations.get("summary", raw_desc))

                    starts_at = parse_alert_time(alert.get("startsAt", ""))
                    ends_at = parse_alert_time(alert.get("endsAt", ""))
                    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    resolved_at = (ends_at or now_str) if event_status == "resolved" else None
                    fingerprint = build_fingerprint(alert, labels, alert_name, cluster, namespace, target)

                    logger.info("   [SQL 准备] 指纹: %s | 触发时间: %s", fingerprint, starts_at)

                    cursor.execute(
                        """
                        SELECT id, first_status, status
                        FROM weekly_alerts
                        WHERE fingerprint = %s AND starts_at <=> %s
                        ORDER BY id DESC
                        LIMIT 1
                        """,
                        (fingerprint, starts_at),
                    )
                    existing_row = cursor.fetchone()

                    if existing_row:
                        alert_id = existing_row[0]
                        cursor.execute(
                            """
                            UPDATE weekly_alerts
                            SET alert_name = %s,
                                cluster = %s,
                                namespace = %s,
                                level = %s,
                                metric_type = %s,
                                target = %s,
                                key_info = %s,
                                detail_info = %s,
                                status = %s,
                                ends_at = %s,
                                first_status = CASE
                                    WHEN COALESCE(first_status, 'firing') = 'resolved' AND %s = 'firing' THEN 'firing'
                                    ELSE COALESCE(first_status, 'firing')
                                END,
                                updated_at = %s,
                                resolved_at = %s
                            WHERE id = %s
                            """,
                            (
                                alert_name,
                                cluster,
                                namespace,
                                level,
                                metric_type,
                                target,
                                key_info,
                                detail_info,
                                event_status,
                                ends_at,
                                event_status,
                                now_str,
                                resolved_at,
                                alert_id,
                            ),
                        )
                        logger.info("🔄 [SQL 执行] 成功更新历史告警状态 ID=%s", alert_id)
                        updated_count += 1
                    else:
                        cursor.execute(
                            """
                            INSERT INTO weekly_alerts
                            (
                                alert_name, cluster, namespace, level, metric_type, target,
                                key_info, detail_info, fingerprint, first_status, status,
                                starts_at, ends_at, created_at, updated_at, resolved_at
                            )
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                            """,
                            (
                                alert_name,
                                cluster,
                                namespace,
                                level,
                                metric_type,
                                target,
                                key_info,
                                detail_info,
                                fingerprint,
                                event_status,
                                event_status,
                                starts_at,
                                ends_at,
                                now_str,
                                now_str,
                                resolved_at,
                            ),
                        )
                        stored_count += 1
                        logger.info("📥 [SQL 执行] 成功落库为新告警记录")

            conn.commit()
        finally:
            conn.close()

        logger.info("🏁 ====== [请求处理成功] 累计落库: %s 条，更新: %s 条 ======\n", stored_count, updated_count)
        return jsonify({"status": "success", "stored": stored_count, "updated": updated_count, "received": len(alerts)}), 200

    except pymysql.MySQLError as sqle:
        logger.error("❌ [SQL 崩溃] 数据库操作失败! 错误明细: %s", sqle, exc_info=True)
        return jsonify({"status": "error", "message": str(sqle)}), 500
    except Exception as exc:
        logger.error("❌ [全局崩溃] Webhook 内部逻辑中断: %s", exc, exc_info=True)
        return jsonify({"status": "error", "message": str(exc)}), 500


init_db()


if __name__ == "__main__":
    logger.info("🚀 告警接收服务已启动，监听端口: %s, db=%s", PORT, get_db_display_name())
    app.run(host="0.0.0.0", port=PORT)
