#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
import datetime
import json
import logging
import os
import sqlite3
import urllib.error
import urllib.request
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
DB_PATH = os.getenv("ALERT_DB_PATH", str(BASE_DIR / "alerts.db"))
LOG_FILE = os.getenv("SHIFT_SUMMARY_LOG_FILE", str(BASE_DIR / "send_dingtalk_shift_summary.log"))
WEBHOOK_URL = os.getenv(
    "DINGTALK_WEBHOOK_URL",
    "https://oapi.dingtalk.com/robot/send?access_token=cb7d9db4acc294387974fed9d9140a3a2c3a324a8b270ff217a557000c89225f",
)
TABLE_NAME = "weekly_alerts"

LEVEL_LABELS = {
    "4": "lv4(紧急)",
    "3": "lv3(严重)",
    "2": "lv2(中度)",
    "1": "lv1(轻微)",
}

logger = logging.getLogger("shift_summary")


def parse_args():
    parser = argparse.ArgumentParser(description="发送早晚班告警统计到钉钉机器人")
    parser.add_argument("--slot", choices=["auto", "morning", "afternoon"], default="auto", help="统计班次")
    parser.add_argument("--run-at", default="", help="指定运行时间，格式如 2026-05-21T09:00:00")
    parser.add_argument("--db-path", default=DB_PATH, help="SQLite 数据库路径")
    parser.add_argument("--webhook", default=WEBHOOK_URL, help="钉钉机器人 webhook")
    parser.add_argument("--dry-run", action="store_true", help="只打印 markdown，不真正发送")
    parser.add_argument("--log-file", default=LOG_FILE, help="日志文件路径")
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"], help="日志级别")
    return parser.parse_args()


def ensure_parent_dir(file_path):
    path_obj = Path(file_path)
    if path_obj.parent and not path_obj.parent.exists():
        path_obj.parent.mkdir(parents=True, exist_ok=True)


def setup_logging(log_file, log_level):
    ensure_parent_dir(log_file)
    logger.setLevel(getattr(logging, log_level.upper(), logging.INFO))
    logger.handlers.clear()

    formatter = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")

    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)


def parse_run_at(run_at_text):
    if not run_at_text:
        return datetime.datetime.now()

    normalized = run_at_text.strip().replace("/", "-")
    if "T" in normalized:
        return datetime.datetime.strptime(normalized, "%Y-%m-%dT%H:%M:%S")
    return datetime.datetime.strptime(normalized, "%Y-%m-%d %H:%M:%S")


def resolve_slot(slot, now_dt):
    if slot != "auto":
        return slot

    if now_dt.hour < 13:
        return "morning"
    return "afternoon"


def build_time_window(slot, now_dt):
    current_date = now_dt.date()

    if slot == "morning":
        start_dt = datetime.datetime.combine(current_date - datetime.timedelta(days=1), datetime.time(17, 0, 0))
        end_dt = datetime.datetime.combine(current_date, datetime.time(8, 59, 59))
        title = "夜班告警统计汇报"
    else:
        start_dt = datetime.datetime.combine(current_date, datetime.time(9, 0, 0))
        end_dt = datetime.datetime.combine(current_date, datetime.time(16, 59, 59))
        title = "白班告警统计汇报"

    return start_dt, end_dt, title


def init_hour_buckets(start_dt, end_dt):
    buckets = []
    current_dt = start_dt.replace(minute=0, second=0, microsecond=0)
    while current_dt <= end_dt:
        buckets.append((current_dt, 0))
        current_dt += datetime.timedelta(hours=1)
    return buckets


def fetch_summary(db_path, start_dt, end_dt):
    if not os.path.exists(db_path):
        raise FileNotFoundError(f"数据库文件不存在: {db_path}")

    start_text = start_dt.strftime("%Y-%m-%d %H:%M:%S")
    end_text = end_dt.strftime("%Y-%m-%d %H:%M:%S")

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    try:
        summary_sql = f"""
        SELECT
            COUNT(*) as total_count,
            SUM(CASE WHEN level = '4' THEN 1 ELSE 0 END) as lv4_count,
            SUM(CASE WHEN level = '3' THEN 1 ELSE 0 END) as lv3_count,
            SUM(CASE WHEN level = '2' THEN 1 ELSE 0 END) as lv2_count,
            SUM(CASE WHEN level = '1' THEN 1 ELSE 0 END) as lv1_count
        FROM {TABLE_NAME}
        WHERE created_at BETWEEN ? AND ?
        """
        cursor.execute(summary_sql, (start_text, end_text))
        row = cursor.fetchone() or (0, 0, 0, 0, 0)

        hourly_sql = f"""
        SELECT substr(created_at, 1, 13) as hour_key, COUNT(*)
        FROM {TABLE_NAME}
        WHERE created_at BETWEEN ? AND ?
        GROUP BY hour_key
        ORDER BY hour_key
        """
        cursor.execute(hourly_sql, (start_text, end_text))
        hourly_rows = cursor.fetchall()

        hourly_map = {item[0]: item[1] for item in hourly_rows}
        hourly_distribution = []
        for bucket_dt, _ in init_hour_buckets(start_dt, end_dt):
            key = bucket_dt.strftime("%Y-%m-%d %H")
            label = bucket_dt.strftime("%m-%d %H:00")
            hourly_distribution.append((label, hourly_map.get(key, 0)))

        return {
            "total_count": row[0] or 0,
            "level_counts": {
                "4": row[1] or 0,
                "3": row[2] or 0,
                "2": row[3] or 0,
                "1": row[4] or 0,
            },
            "hourly_distribution": hourly_distribution,
        }
    finally:
        conn.close()


def format_text_table(headers, rows):
    widths = [len(header) for header in headers]
    normalized_rows = [[str(cell) for cell in row] for row in rows]

    for row in normalized_rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(cell))

    def render_row(row_values):
        return "  ".join(value.ljust(widths[index]) for index, value in enumerate(row_values))

    lines = [render_row(headers), render_row(["-" * width for width in widths])]
    for row in normalized_rows:
        lines.append(render_row(row))
    return "\n".join(lines)


def build_markdown(title, start_dt, end_dt, summary_data):
    overview_rows = [
        ("告警总数", f"{summary_data['total_count']} 条"),
    ]
    level_rows = [
        (LEVEL_LABELS["4"], f"{summary_data['level_counts']['4']} 条"),
        (LEVEL_LABELS["3"], f"{summary_data['level_counts']['3']} 条"),
        (LEVEL_LABELS["2"], f"{summary_data['level_counts']['2']} 条"),
        (LEVEL_LABELS["1"], f"{summary_data['level_counts']['1']} 条"),
    ]
    hourly_rows = [(item[0], f"{item[1]} 条") for item in summary_data["hourly_distribution"]]

    markdown_lines = [
        f"## {title}",
        "",
        f"**统计时间：** {start_dt.strftime('%Y-%m-%d %H:%M:%S')} - {end_dt.strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        "### 总体概览",
        "```text",
        format_text_table(["指标", "数量"], overview_rows),
        "```",
        "",
        "### 按等级分布",
        "```text",
        format_text_table(["等级", "数量"], level_rows),
        "```",
        "",
        "### 按小时分布",
        "```text",
        format_text_table(["时间", "告警数"], hourly_rows),
        "```",
    ]
    return "\n".join(markdown_lines)


def send_to_dingtalk(webhook, title, markdown_text):
    payload = {
        "msgtype": "markdown",
        "markdown": {
            "title": title,
            "text": markdown_text,
        },
    }
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        webhook,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            raw_body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        response_body = exc.read().decode("utf-8", errors="ignore")
        raise RuntimeError(f"钉钉接口返回 HTTP {exc.code}: {response_body}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"请求钉钉机器人失败: {exc}") from exc

    logger.info("钉钉响应: %s", raw_body)
    parsed = json.loads(raw_body)
    if parsed.get("errcode") != 0:
        raise RuntimeError(f"钉钉机器人发送失败: {raw_body}")


def main():
    args = parse_args()
    setup_logging(args.log_file, args.log_level)

    now_dt = parse_run_at(args.run_at)
    slot = resolve_slot(args.slot, now_dt)
    start_dt, end_dt, title = build_time_window(slot, now_dt)

    logger.info(
        "开始执行钉钉值班汇总：slot=%s, run_at=%s, window=%s ~ %s, db=%s, dry_run=%s",
        slot,
        now_dt.strftime("%Y-%m-%d %H:%M:%S"),
        start_dt.strftime("%Y-%m-%d %H:%M:%S"),
        end_dt.strftime("%Y-%m-%d %H:%M:%S"),
        args.db_path,
        args.dry_run,
    )

    summary_data = fetch_summary(args.db_path, start_dt, end_dt)
    markdown_text = build_markdown(title, start_dt, end_dt, summary_data)

    logger.info(
        "统计完成：总数=%s, lv4=%s, lv3=%s, lv2=%s, lv1=%s",
        summary_data["total_count"],
        summary_data["level_counts"]["4"],
        summary_data["level_counts"]["3"],
        summary_data["level_counts"]["2"],
        summary_data["level_counts"]["1"],
    )

    if args.dry_run:
        print(markdown_text)
        logger.info("DRY RUN 模式，仅打印 markdown，不发送钉钉")
        return

    send_to_dingtalk(args.webhook, title, markdown_text)
    logger.info("钉钉发送成功：%s", title)


if __name__ == "__main__":
    main()
