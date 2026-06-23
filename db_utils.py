import logging
import os
from datetime import date, datetime
from pathlib import Path

import pymysql


BASE_DIR = Path(__file__).resolve().parent
MYSQL_HOST = os.getenv("ALERT_DB_HOST", "172.21.8.102")
MYSQL_PORT = int(os.getenv("ALERT_DB_PORT", "27641"))
MYSQL_USER = os.getenv("ALERT_DB_USER", "root")
MYSQL_PASSWORD = os.getenv("ALERT_DB_PASSWORD", "HkIm8421124C")
MYSQL_DATABASE = os.getenv("ALERT_DB_NAME", "alert_stat")
TABLE_NAME = "weekly_alerts"


def get_db_display_name():
    return f"{MYSQL_USER}@{MYSQL_HOST}:{MYSQL_PORT}/{MYSQL_DATABASE}"


def quote_identifier(name):
    if not name or any(ch not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_" for ch in name):
        raise ValueError(f"非法数据库标识符: {name}")
    return f"`{name}`"


def connect_server(autocommit=True):
    return pymysql.connect(
        host=MYSQL_HOST,
        port=MYSQL_PORT,
        user=MYSQL_USER,
        password=MYSQL_PASSWORD,
        charset="utf8mb4",
        cursorclass=pymysql.cursors.Cursor,
        autocommit=autocommit,
    )


def get_db_connection(dict_cursor=False, autocommit=False):
    cursor_class = pymysql.cursors.DictCursor if dict_cursor else pymysql.cursors.Cursor
    return pymysql.connect(
        host=MYSQL_HOST,
        port=MYSQL_PORT,
        user=MYSQL_USER,
        password=MYSQL_PASSWORD,
        database=MYSQL_DATABASE,
        charset="utf8mb4",
        cursorclass=cursor_class,
        autocommit=autocommit,
    )


def ensure_database_exists():
    server_conn = connect_server(autocommit=True)
    try:
        with server_conn.cursor() as cursor:
            cursor.execute(
                f"CREATE DATABASE IF NOT EXISTS {quote_identifier(MYSQL_DATABASE)} "
                "DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
            )
    finally:
        server_conn.close()


def get_table_columns(cursor, table_name):
    cursor.execute(
        """
        SELECT COLUMN_NAME
        FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s
        """,
        (MYSQL_DATABASE, table_name),
    )
    return {row[0] for row in cursor.fetchall()}


def column_exists(cursor, table_name, column_name):
    return column_name in get_table_columns(cursor, table_name)


def ensure_column(cursor, table_name, column_name, definition):
    if column_exists(cursor, table_name, column_name):
        return

    cursor.execute(
        f"ALTER TABLE {quote_identifier(table_name)} "
        f"ADD COLUMN {quote_identifier(column_name)} {definition}"
    )


def index_exists(cursor, table_name, index_name):
    cursor.execute(
        """
        SELECT 1
        FROM information_schema.STATISTICS
        WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s AND INDEX_NAME = %s
        LIMIT 1
        """,
        (MYSQL_DATABASE, table_name, index_name),
    )
    return cursor.fetchone() is not None


def ensure_index(cursor, table_name, index_name, column_name):
    if index_exists(cursor, table_name, index_name):
        return

    cursor.execute(
        f"CREATE INDEX {quote_identifier(index_name)} "
        f"ON {quote_identifier(table_name)} ({quote_identifier(column_name)})"
    )


def init_db_schema(logger=None):
    schema_logger = logger or logging.getLogger(__name__)
    ensure_database_exists()

    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {quote_identifier(TABLE_NAME)} (
                    id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
                    alert_name VARCHAR(255),
                    cluster VARCHAR(255),
                    namespace VARCHAR(255),
                    level VARCHAR(32),
                    metric_type VARCHAR(64),
                    target VARCHAR(255),
                    key_info TEXT,
                    detail_info TEXT,
                    fingerprint VARCHAR(255),
                    first_status VARCHAR(32) DEFAULT 'firing',
                    status VARCHAR(32) DEFAULT 'firing',
                    starts_at DATETIME NULL,
                    ends_at DATETIME NULL,
                    created_at DATETIME NULL,
                    updated_at DATETIME NULL,
                    resolved_at DATETIME NULL,
                    remark TEXT,
                    remark_updated_at DATETIME NULL
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
                """
            )

            ensure_column(cursor, TABLE_NAME, "fingerprint", "VARCHAR(255)")
            ensure_column(cursor, TABLE_NAME, "first_status", "VARCHAR(32) DEFAULT 'firing'")
            ensure_column(cursor, TABLE_NAME, "status", "VARCHAR(32) DEFAULT 'firing'")
            ensure_column(cursor, TABLE_NAME, "ends_at", "DATETIME NULL")
            ensure_column(cursor, TABLE_NAME, "updated_at", "DATETIME NULL")
            ensure_column(cursor, TABLE_NAME, "resolved_at", "DATETIME NULL")
            ensure_column(cursor, TABLE_NAME, "remark", "TEXT")
            ensure_column(cursor, TABLE_NAME, "remark_updated_at", "DATETIME NULL")

            cursor.execute(
                f"""
                UPDATE {quote_identifier(TABLE_NAME)}
                SET first_status = 'firing'
                WHERE first_status IS NULL OR first_status = ''
                """
            )
            cursor.execute(
                f"""
                UPDATE {quote_identifier(TABLE_NAME)}
                SET status = 'firing'
                WHERE status IS NULL OR status = ''
                """
            )
            cursor.execute(
                f"""
                UPDATE {quote_identifier(TABLE_NAME)}
                SET updated_at = created_at
                WHERE updated_at IS NULL
                """
            )

            ensure_index(cursor, TABLE_NAME, "idx_weekly_alerts_created_at", "created_at")
            ensure_index(cursor, TABLE_NAME, "idx_weekly_alerts_status", "status")
            ensure_index(cursor, TABLE_NAME, "idx_weekly_alerts_starts_at", "starts_at")
            ensure_index(cursor, TABLE_NAME, "idx_weekly_alerts_fingerprint", "fingerprint")

        conn.commit()
        schema_logger.info("MySQL 数据库初始化完成: %s", get_db_display_name())
    finally:
        conn.close()


def format_db_value(value):
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(value, date):
        return value.strftime("%Y-%m-%d")
    return value


def row_to_dict(row):
    return {key: format_db_value(value) for key, value in row.items()}
