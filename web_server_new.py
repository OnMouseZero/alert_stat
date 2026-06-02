import sqlite3
import datetime
import hashlib
import json
import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path
from flask import Flask, request, jsonify

# --- 配置 ---
BASE_DIR = Path(__file__).resolve().parent
DB_FILE = str(Path(os.getenv("ALERT_DB_PATH", str(BASE_DIR / "alerts.db"))))
PORT = 5001
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
        ensure_column(c, 'weekly_alerts', 'remark', 'TEXT')
        ensure_column(c, 'weekly_alerts', 'remark_updated_at', 'TEXT')

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
    # 🌟 探测点 1：只要有任何请求进门，立刻打一杆子
    logger.info("====== [Webhook 收到新请求] ======")
    logger.info(f"请求源 IP: {request.remote_addr} | 请求头: {dict(request.headers)}")

    try:
        data = request.json
        if not data:
            logger.warning("⚠️ 收到空请求，或者 Content-Type 不是 application/json")
            return "No JSON data received", 400

        # 🌟 探测点 2：打印原始数据的结构摘要，看看推过来了多少条告警
        alerts = data.get('alerts', [])
        logger.info(f"📊 成功解析 JSON 负载。包含的告警条数: {len(alerts)}")

        # 如果你想看完整的原始 JSON 数据，可以取消下面这行的注释（告警多时会刷屏）
        # logger.info(f"原始数据明细: {json.dumps(data, ensure_ascii=False)}")

        stored_count = 0
        updated_count = 0

        conn = get_db_connection()
        c = conn.cursor()

        for index, alert in enumerate(alerts):
            event_status = str(alert.get('status', 'firing')).lower()
            labels = alert.get('labels', {})
            annotations = alert.get('annotations', {})

            alert_name = labels.get('metricName', labels.get('alertname', 'Unknown Alert'))
            cluster = labels.get('clusterName', labels.get('cluster', 'default'))
            level = labels.get('alertLevel', '0')
            target = labels.get('alertTarget', labels.get('instance', 'Unknown'))

            # 🌟 探测点 3：打印当前正在处理的每一条具体告警明细
            logger.info(f"-> 正在处理第 {index+1} 条: [{cluster}] {alert_name} | 状态: {event_status} | 级别: {level} | 对象: {target}")

            if event_status not in {'firing', 'resolved'}:
                logger.warning(f"⚠️ 忽略未知状态告警: {event_status}")
                continue

            namespace = labels.get('namespace', '-')
            metric_type = labels.get('metricType', '通用')
            raw_desc = annotations.get('description', '')
            key_info = annotations.get('alertPoint', raw_desc[:50] + '...' if len(raw_desc) > 50 else raw_desc)
            detail_info = annotations.get('alertContent', annotations.get('summary', raw_desc))

            starts_at = parse_alert_time(alert.get('startsAt', ''))
            ends_at = parse_alert_time(alert.get('endsAt', ''))
            now_str = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            resolved_at = ends_at or now_str if event_status == 'resolved' else None
            fingerprint = build_fingerprint(alert, labels, alert_name, cluster, namespace, target)

            # 🌟 探测点 4：打印指纹和开始时间，看能否在数据库匹配到历史记录
            logger.info(f"   [SQL 准备] 指纹: {fingerprint} | 触发时间: {starts_at}")

            try:
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
                    logger.info(f"🔄 [SQL 执行] 成功更新历史告警状态 ID={alert_id}")
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
                    logger.info("📥 [SQL 执行] 成功落库为新告警记录")

            except sqlite3.Error as sqle:
                # 🌟 探测点 5：精准捕获数据库层面的崩溃（例如表结构对不上、字段不存在等）
                logger.error(f"❌ [SQL 崩溃] 数据库操作失败! 错误明细: {sqle}")
                raise sqle

        conn.commit()
        conn.close()

        logger.info(f"🏁 ====== [请求处理成功] 累计落库: {stored_count} 条，更新: {updated_count} 条 ======\n")
        return jsonify({"status": "success", "stored": stored_count, "updated": updated_count, "received": len(alerts)}), 200

    except Exception as e:
        # 🌟 探测点 6：全局兜底报错
        logger.error(f"❌ [全局崩溃] Webhook 内部逻辑中断: {e}", exc_info=True)
        return jsonify({"status": "error", "message": str(e)}), 500


if __name__ == '__main__':
    # 建议先手动删除旧的 alerts.db 文件，否则可能会报错 table 已存在但列不对
    if os.path.exists(DB_FILE):
        # 简单检查一下是否需要重新初始化（可选逻辑，这里简单起见建议手动处理）
        pass
        
    init_db()
    logger.info(f"🚀 告警接收服务已启动，监听端口: {PORT}...")
    app.run(host='0.0.0.0', port=PORT)
