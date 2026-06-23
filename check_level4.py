#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
import logging
import os
import smtplib
import time
from email.header import Header
from email.mime.text import MIMEText
from pathlib import Path

from db_utils import get_db_connection, get_db_display_name


BASE_DIR = Path(__file__).resolve().parent
TABLE_NAME = "weekly_alerts"
SENT_FILE = os.getenv("ALERT_SENT_FILE", str(BASE_DIR / "sent_level4_ids.txt"))
LOG_FILE = os.getenv("ALERT_LOG_FILE", str(BASE_DIR / "check_level4.log"))

SMTP_SERVER = "smtp.mxhichina.com"
SMTP_PORT = 465
FROM_EMAIL = "monitor@cdscwl.cn"
SMTP_USER = "monitor@cdscwl.cn"
SMTP_PASS = "xdaZVclOj7RrB6nw"
TO_EMAILS_RAW = os.getenv("ALERT_LEVEL4_TO_EMAILS", "liud@cdscwl.cn")
TO_EMAILS = [item.strip() for item in TO_EMAILS_RAW.split(",") if item.strip()]

logger = logging.getLogger("check_level4")


def parse_args():
    parser = argparse.ArgumentParser(description="扫描并发送 4 级紧急告警邮件")
    parser.add_argument("--once", action="store_true", help="只执行一次查询和发送，不进入循环")
    parser.add_argument("--interval", type=int, default=600, help="循环模式下的扫描间隔秒数，默认 600 秒")
    parser.add_argument("--lookback-hours", type=int, default=48, help="向前查询最近多少小时的 4 级未恢复告警，默认 48 小时")
    parser.add_argument("--dry-run", action="store_true", help="只打印待发送告警，不真正发邮件")
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

    logger.info("日志初始化完成，日志文件: %s", log_file)


def load_sent_ids():
    if not os.path.exists(SENT_FILE):
        logger.info("去重文件不存在，将从空记录开始: %s", SENT_FILE)
        return set()

    with open(SENT_FILE, "r", encoding="utf-8") as file_obj:
        sent_ids = set(line.strip() for line in file_obj if line.strip())
    logger.info("已加载去重记录 %s 条: %s", len(sent_ids), SENT_FILE)
    return sent_ids


def save_sent_id(alert_id):
    ensure_parent_dir(SENT_FILE)
    with open(SENT_FILE, "a", encoding="utf-8") as file_obj:
        file_obj.write(str(alert_id) + "\n")
    logger.debug("已写入去重记录 alert_id=%s 到 %s", alert_id, SENT_FILE)


def query_level4_alerts(lookback_hours):
    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            sql = f"""
            SELECT id, alert_name, cluster, namespace, level,
                   metric_type, target, key_info, detail_info,
                   starts_at, created_at
            FROM {TABLE_NAME}
            WHERE created_at >= DATE_SUB(NOW(), INTERVAL %s HOUR)
              AND COALESCE(first_status, 'firing') = 'firing'
              AND COALESCE(status, 'firing') = 'firing'
              AND level IN ('4', 'emergency', 'critical')
            ORDER BY id DESC
            """
            cursor.execute(sql, (lookback_hours,))
            rows = cursor.fetchall()
    finally:
        conn.close()

    logger.info(
        "查询到等级4未恢复告警 %s 条，数据库=%s，回看窗口=%s 小时",
        len(rows),
        get_db_display_name(),
        lookback_hours,
    )
    return rows


def build_mail_content(row):
    (
        alert_id,
        alert_name,
        cluster,
        namespace,
        level,
        metric_type,
        target,
        key_info,
        detail_info,
        starts_at,
        created_at,
    ) = row

    subject = f"[等级4紧急告警] {alert_name}"
    html = f"""
    <html>
    <body style="font-family:Arial,Helvetica,sans-serif;background:#f6f8fa;padding:20px;">
      <div style="max-width:720px;background:#ffffff;border-radius:10px;padding:24px;border:1px solid #e5e7eb;">
        <h2 style="color:#d9001b;margin-top:0;">Prometheus 紧急告警通知</h2>
        <p style="font-size:16px;"><b>告警名称：</b>{alert_name}</p>
        <table style="border-collapse:collapse;width:100%;font-size:14px;">
          <tr><td style="padding:8px;border:1px solid #ddd;"><b>告警ID</b></td><td style="padding:8px;border:1px solid #ddd;">{alert_id}</td></tr>
          <tr><td style="padding:8px;border:1px solid #ddd;"><b>等级</b></td><td style="padding:8px;border:1px solid #ddd;color:red;"><b>{level}</b></td></tr>
          <tr><td style="padding:8px;border:1px solid #ddd;"><b>集群</b></td><td style="padding:8px;border:1px solid #ddd;">{cluster}</td></tr>
          <tr><td style="padding:8px;border:1px solid #ddd;"><b>命名空间</b></td><td style="padding:8px;border:1px solid #ddd;">{namespace}</td></tr>
          <tr><td style="padding:8px;border:1px solid #ddd;"><b>指标类型</b></td><td style="padding:8px;border:1px solid #ddd;">{metric_type}</td></tr>
          <tr><td style="padding:8px;border:1px solid #ddd;"><b>目标对象</b></td><td style="padding:8px;border:1px solid #ddd;">{target}</td></tr>
        </table>
        <h3 style="margin-top:22px;">关键信息</h3>
        <div style="background:#fff7e6;padding:12px;border-left:4px solid #fa8c16;">{key_info or ''}</div>
        <h3 style="margin-top:22px;">详细信息</h3>
        <pre style="background:#f5f5f5;padding:12px;border-radius:6px;white-space:pre-wrap;">{detail_info or ''}</pre>
        <h3 style="margin-top:22px;">时间信息</h3>
        <p>开始时间：{starts_at}<br>入库时间：{created_at}</p>
        <div style="margin-top:28px;padding:14px;background:#fff1f0;border-left:5px solid #ff4d4f;font-size:16px;">请立即处理！</div>
      </div>
    </body>
    </html>
    """
    return subject, html


def send_mail(row, dry_run=False):
    subject, html = build_mail_content(row)
    alert_id = row[0]
    alert_name = row[1]

    if dry_run:
        logger.info("DRY RUN 邮件预览: alert_id=%s, subject=%s", alert_id, subject)
        return

    logger.info("准备发送邮件: alert_id=%s, alert_name=%s, smtp=%s:%s", alert_id, alert_name, SMTP_SERVER, SMTP_PORT)
    msg = MIMEText(html, "html", "utf-8")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = FROM_EMAIL
    msg["To"] = ", ".join(TO_EMAILS)

    server = smtplib.SMTP_SSL(SMTP_SERVER, SMTP_PORT, timeout=15)
    server.login(SMTP_USER, SMTP_PASS)
    server.sendmail(FROM_EMAIL, TO_EMAILS, msg.as_string())
    server.quit()

    logger.info("邮件发送成功: alert_id=%s, subject=%s, to=%s", alert_id, subject, ",".join(TO_EMAILS))


def run_once(args):
    sent_ids = load_sent_ids()
    rows = query_level4_alerts(args.lookback_hours)
    logger.info("本次开始处理，待发送候选=%s，数据库=%s", len(rows), get_db_display_name())

    sent_count = 0
    skipped_count = 0

    for row in rows:
        alert_id = str(row[0])
        if alert_id in sent_ids:
            skipped_count += 1
            logger.debug("跳过已发送告警: alert_id=%s, alert_name=%s", alert_id, row[1])
            continue

        send_mail(row, dry_run=args.dry_run)
        if not args.dry_run:
            save_sent_id(alert_id)
        sent_count += 1

    logger.info(
        "本次处理完成：发送 %s 条，跳过 %s 条，去重文件=%s",
        sent_count,
        skipped_count,
        SENT_FILE,
    )


def main():
    args = parse_args()
    setup_logging(args.log_file, args.log_level)
    logger.info(
        "启动参数：once=%s, interval=%s, lookback_hours=%s, dry_run=%s, db=%s, sent_file=%s",
        args.once,
        args.interval,
        args.lookback_hours,
        args.dry_run,
        get_db_display_name(),
        SENT_FILE,
    )

    if args.once:
        try:
            run_once(args)
        except Exception:
            logger.exception("单次执行失败")
        return

    while True:
        try:
            run_once(args)
        except Exception:
            logger.exception("循环执行失败")
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
