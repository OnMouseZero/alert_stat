import sqlite3
import datetime
import hashlib
import json
import logging
import os
from flask import Flask, request, jsonify

# --- 配置 ---
DB_FILE = 'alerts.db'
PORT = 5001

# 配置日志
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s')
logger = logging.getLogger()

app = Flask(__name__)


def get_db_connection():
    return sqlite3.connect(DB_FILE)


def column_exists(cursor, table_name, column_name):
    cursor.execute(f"PRAGMA table_info({table_name})")
    return any(row[1] == column_name for row in cursor.fetchall())


def ensure_column(cursor, table_name, column_name, definition):
    if not column_exists(cursor, table_name, column_name):
        cursor.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {definition}")


def parse_alert_time(raw_value):
    """将 Alertmanager 的 ISO 时间尽量转为本地可读格式。"""
    if not raw_value:
        return ""

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
        return raw_value


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
    """
    初始化数据库
    重新规划了字段，与 DingTalk 模板对齐，确保核心信息完整
    """
    # 建议：如果字段变动较大，手动删除旧 db 文件或做迁移。这里为了演示简单，建议您先删除旧 alerts.db
    
    try:
        conn = get_db_connection()
        c = conn.cursor()
        
        # 核心字段解释：
        # alert_name  : 对应 metricName (如 Node内存使用率)
        # cluster     : 对应 clusterName (如 生产-Cluster)
        # namespace   : 对应 namespace (如 monitoring)
        # level       : 对应 alertLevel (如 1, 2, 3, 4)
        # metric_type : 对应 metricType (如 资源, 业务)
        # target      : 对应 alertTarget (如 192.168.1.10)
        # key_info    : 对应 alertPoint (简短的关键信息，用于 PDF 表格展示，避免截断)
        # detail_info : 对应 alertContent (详细规则描述，用于 PDF 详情部分)
        
        c.execute('''CREATE TABLE IF NOT EXISTS weekly_alerts (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        alert_name TEXT,
                        cluster TEXT,
                        namespace TEXT,
                        level TEXT,
                        metric_type TEXT,
                        target TEXT,
                        key_info TEXT,
                        detail_info TEXT,
                        fingerprint TEXT,
                        first_status TEXT DEFAULT 'firing',
                        status TEXT DEFAULT 'firing',
                        starts_at TEXT,
                        ends_at TEXT,
                        created_at TEXT,
                        updated_at TEXT,
                        resolved_at TEXT
                    )''')

        ensure_column(c, 'weekly_alerts', 'fingerprint', 'TEXT')
        ensure_column(c, 'weekly_alerts', 'first_status', "TEXT DEFAULT 'firing'")
        ensure_column(c, 'weekly_alerts', 'status', "TEXT DEFAULT 'firing'")
        ensure_column(c, 'weekly_alerts', 'ends_at', 'TEXT')
        ensure_column(c, 'weekly_alerts', 'updated_at', 'TEXT')
        ensure_column(c, 'weekly_alerts', 'resolved_at', 'TEXT')

        c.execute("UPDATE weekly_alerts SET first_status = 'firing' WHERE first_status IS NULL OR first_status = ''")
        c.execute("UPDATE weekly_alerts SET status = 'firing' WHERE status IS NULL OR status = ''")
        c.execute("UPDATE weekly_alerts SET updated_at = created_at WHERE updated_at IS NULL OR updated_at = ''")

        c.execute("CREATE INDEX IF NOT EXISTS idx_weekly_alerts_created_at ON weekly_alerts(created_at)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_weekly_alerts_status ON weekly_alerts(status)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_weekly_alerts_starts_at ON weekly_alerts(starts_at)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_weekly_alerts_fingerprint ON weekly_alerts(fingerprint)")
        conn.commit()
        conn.close()
        logger.info(f"✅ 数据库 {DB_FILE} 初始化完成 (Schema 已升级)")
    except Exception as e:
        logger.error(f"❌ 数据库初始化失败: {e}")

@app.route('/health', methods=['GET'])
def health_check():
    return "I am alive!", 200

@app.route('/webhook', methods=['POST'])
def webhook():
    """接收 Alertmanager 发来的 JSON 并映射到新字段"""
    try:
        data = request.json
        if not data:
            return "No JSON data received", 400

        alerts = data.get('alerts', [])
        stored_count = 0
        updated_count = 0
        
        conn = get_db_connection()
        c = conn.cursor()

        for alert in alerts:
            event_status = str(alert.get('status', 'firing')).lower()
            if event_status not in {'firing', 'resolved'}:
                logger.warning(f"⚠️ 忽略未知状态告警: {event_status}")
                continue

            labels = alert.get('labels', {})
            annotations = alert.get('annotations', {})

            alert_name = labels.get('metricName', labels.get('alertname', 'Unknown Alert'))
            cluster = labels.get('clusterName', labels.get('cluster', 'default'))
            namespace = labels.get('namespace', '-')
            level = labels.get('alertLevel', '0')
            metric_type = labels.get('metricType', '通用')
            target = labels.get('alertTarget', labels.get('instance', 'Unknown'))

            raw_desc = annotations.get('description', '')
            key_info = annotations.get('alertPoint', raw_desc[:50] + '...' if len(raw_desc) > 50 else raw_desc)
            detail_info = annotations.get('alertContent', annotations.get('summary', raw_desc))

            starts_at = parse_alert_time(alert.get('startsAt', ''))
            ends_at = parse_alert_time(alert.get('endsAt', ''))
            now_str = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            resolved_at = ends_at or now_str if event_status == 'resolved' else None
            fingerprint = build_fingerprint(alert, labels, alert_name, cluster, namespace, target)

            c.execute(
                '''SELECT id, first_status, status FROM weekly_alerts
                   WHERE fingerprint = ? AND starts_at = ?
                   ORDER BY id DESC LIMIT 1''',
                (fingerprint, starts_at)
            )
            existing_row = c.fetchone()

            if existing_row:
                alert_id = existing_row[0]
                c.execute(
                    '''UPDATE weekly_alerts
                       SET alert_name = ?, cluster = ?, namespace = ?, level = ?, metric_type = ?,
                           target = ?, key_info = ?, detail_info = ?, status = ?, ends_at = ?,
                           first_status = CASE
                               WHEN COALESCE(first_status, 'firing') = 'resolved' AND ? = 'firing' THEN 'firing'
                               ELSE COALESCE(first_status, 'firing')
                           END,
                           updated_at = ?, resolved_at = ?
                       WHERE id = ?''',
                    (
                        alert_name, cluster, namespace, level, metric_type,
                        target, key_info, detail_info, event_status, ends_at,
                        event_status, now_str, resolved_at, alert_id
                    )
                )
                logger.info(f"🔄 更新告警状态: [{cluster}] {alert_name} -> {event_status}")
                updated_count += 1
            else:
                c.execute(
                    '''INSERT INTO weekly_alerts
                       (alert_name, cluster, namespace, level, metric_type, target, key_info,
                        detail_info, fingerprint, first_status, status, starts_at, ends_at,
                        created_at, updated_at, resolved_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                    (
                        alert_name, cluster, namespace, level, metric_type, target, key_info,
                        detail_info, fingerprint, event_status, event_status, starts_at, ends_at,
                        now_str, now_str, resolved_at
                    )
                )
                stored_count += 1
                logger.info(f"📥 新增告警记录: [{cluster}] {alert_name} ({event_status}, Lv.{level})")

        conn.commit()
        conn.close()
        return jsonify({"status": "success", "stored": stored_count, "updated": updated_count, "received": len(alerts)}), 200

    except Exception as e:
        logger.error(f"❌ 出错: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500

if __name__ == '__main__':
    # 建议先手动删除旧的 alerts.db 文件，否则可能会报错 table 已存在但列不对
    if os.path.exists(DB_FILE):
        # 简单检查一下是否需要重新初始化（可选逻辑，这里简单起见建议手动处理）
        pass
        
    init_db()
    logger.info(f"🚀 告警接收服务已启动，监听端口: {PORT}...")
    app.run(host='0.0.0.0', port=PORT)
