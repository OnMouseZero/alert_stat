import sqlite3
from datetime import datetime

from db_utils import TABLE_NAME, get_db_connection, init_db_schema


SQLITE_DB = "alerts.db"
BATCH_SIZE = 1000


def normalize_datetime(value):
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    return str(value).strip() or None


def migrate():
    init_db_schema()

    sqlite_conn = sqlite3.connect(SQLITE_DB)
    sqlite_conn.row_factory = sqlite3.Row
    mysql_conn = get_db_connection()

    migrated = 0
    skipped = 0

    try:
        sqlite_cursor = sqlite_conn.cursor()
        mysql_cursor = mysql_conn.cursor()

        sqlite_cursor.execute(f"SELECT * FROM {TABLE_NAME} ORDER BY id ASC")

        while True:
            rows = sqlite_cursor.fetchmany(BATCH_SIZE)
            if not rows:
                break

            for row in rows:
                mysql_cursor.execute(
                    f"SELECT id FROM {TABLE_NAME} WHERE id = %s LIMIT 1",
                    (row["id"],),
                )
                if mysql_cursor.fetchone():
                    skipped += 1
                    continue

                mysql_cursor.execute(
                    f"""
                    INSERT INTO {TABLE_NAME}
                    (
                        id, alert_name, cluster, namespace, level, metric_type, target,
                        key_info, detail_info, fingerprint, first_status, status,
                        starts_at, ends_at, created_at, updated_at, resolved_at,
                        remark, remark_updated_at
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        row["id"],
                        row["alert_name"],
                        row["cluster"],
                        row["namespace"],
                        row["level"],
                        row["metric_type"],
                        row["target"],
                        row["key_info"],
                        row["detail_info"],
                        row["fingerprint"],
                        row["first_status"],
                        row["status"],
                        normalize_datetime(row["starts_at"]),
                        normalize_datetime(row["ends_at"]),
                        normalize_datetime(row["created_at"]),
                        normalize_datetime(row["updated_at"]),
                        normalize_datetime(row["resolved_at"]),
                        row["remark"] if "remark" in row.keys() else None,
                        normalize_datetime(row["remark_updated_at"]) if "remark_updated_at" in row.keys() else None,
                    ),
                )
                migrated += 1

        mysql_conn.commit()
        mysql_cursor.execute(f"SELECT COALESCE(MAX(id), 0) FROM {TABLE_NAME}")
        max_id = mysql_cursor.fetchone()[0] or 0
        mysql_cursor.execute(f"ALTER TABLE {TABLE_NAME} AUTO_INCREMENT = %s", (max_id + 1,))
        mysql_conn.commit()
    finally:
        sqlite_conn.close()
        mysql_conn.close()

    print(f"迁移完成: 新增 {migrated} 条, 跳过 {skipped} 条")


if __name__ == "__main__":
    migrate()
