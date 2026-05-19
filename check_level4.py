#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import sqlite3
import os
import smtplib
import time
from email.mime.text import MIMEText
from email.header import Header

# ==================================================
# 配置区（按你的实际信息已填好）
# ==================================================
DB_PATH = "/root/doge/inspection/alerts.db"
TABLE_NAME = "weekly_alerts"

# 已发送记录文件（避免重复发）
SENT_FILE = "/root/doge/inspection/sent_level4_ids.txt"

# 阿里企业邮箱 SMTP
SMTP_SERVER = "smtp.mxhichina.com"
SMTP_PORT = 465

FROM_EMAIL = "monitor@cdscwl.cn"
SMTP_USER = "monitor@cdscwl.cn"
SMTP_PASS = "xdaZVclOj7RrB6nw"

TO_EMAIL = "liud@cdscwl.cn"


# ==================================================
# 读取已发送ID
# ==================================================
def load_sent_ids():
    if not os.path.exists(SENT_FILE):
        return set()

    with open(SENT_FILE, "r", encoding="utf-8") as f:
        return set(line.strip() for line in f if line.strip())


def save_sent_id(alert_id):
    with open(SENT_FILE, "a", encoding="utf-8") as f:
        f.write(str(alert_id) + "\n")


# ==================================================
# 查询等级4紧急告警
# ==================================================
def query_level4_alerts():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    sql = f"""
    SELECT id, alert_name, cluster, namespace, level,
       metric_type, target, key_info, detail_info,
       starts_at, created_at
       FROM weekly_alerts
       WHERE datetime(created_at) >= datetime('now', '-2 day')
       AND COALESCE(first_status, 'firing') = 'firing'
       AND COALESCE(status, 'firing') = 'firing'
       AND level IN ('4', 'emergency', 'critical')
       ORDER BY id DESC;
    """

    cursor.execute(sql)
    rows = cursor.fetchall()
    conn.close()
    return rows


# ==================================================
# 发邮件
# ==================================================
def send_mail(row):
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
        created_at
    ) = row

    subject = f"[等级4紧急告警] {alert_name}"

    html = f"""
    <html>
    <body style="font-family:Arial,Helvetica,sans-serif;background:#f6f8fa;padding:20px;">

    <div style="max-width:720px;background:#ffffff;border-radius:10px;
                padding:24px;border:1px solid #e5e7eb;">

        <h2 style="color:#d9001b;margin-top:0;">
        🚨 Prometheus 紧急告警通知
        </h2>

        <p style="font-size:16px;">
        <b>告警名称：</b>{alert_name}
        </p>

        <table style="border-collapse:collapse;width:100%;font-size:14px;">
            <tr>
                <td style="padding:8px;border:1px solid #ddd;"><b>告警ID</b></td>
                <td style="padding:8px;border:1px solid #ddd;">{alert_id}</td>
            </tr>
            <tr>
                <td style="padding:8px;border:1px solid #ddd;"><b>等级</b></td>
                <td style="padding:8px;border:1px solid #ddd;color:red;"><b>{level}</b></td>
            </tr>
            <tr>
                <td style="padding:8px;border:1px solid #ddd;"><b>集群</b></td>
                <td style="padding:8px;border:1px solid #ddd;">{cluster}</td>
            </tr>
            <tr>
                <td style="padding:8px;border:1px solid #ddd;"><b>命名空间</b></td>
                <td style="padding:8px;border:1px solid #ddd;">{namespace}</td>
            </tr>
            <tr>
                <td style="padding:8px;border:1px solid #ddd;"><b>指标类型</b></td>
                <td style="padding:8px;border:1px solid #ddd;">{metric_type}</td>
            </tr>
            <tr>
                <td style="padding:8px;border:1px solid #ddd;"><b>目标对象</b></td>
                <td style="padding:8px;border:1px solid #ddd;">{target}</td>
            </tr>
        </table>

        <h3 style="margin-top:22px;">📌 关键信息</h3>
        <div style="background:#fff7e6;padding:12px;border-left:4px solid #fa8c16;">
        {key_info}
        </div>

        <h3 style="margin-top:22px;">📄 详细信息</h3>
        <pre style="background:#f5f5f5;padding:12px;border-radius:6px;
white-space:pre-wrap;">{detail_info}</pre>

        <h3 style="margin-top:22px;">⏰ 时间信息</h3>
        <p>
        开始时间：{starts_at}<br>
        入库时间：{created_at}
        </p>

        <div style="margin-top:28px;padding:14px;
                    background:#fff1f0;border-left:5px solid #ff4d4f;
                    font-size:16px;">
        ⚠ 请立即处理！
        </div>

        <p style="margin-top:25px;color:#999;font-size:12px;">
        Prometheus Alert Robot 自动发送
        </p>

    </div>
    </body>
    </html>
    """

    msg = MIMEText(html, "html", "utf-8")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = FROM_EMAIL
    msg["To"] = TO_EMAIL

    server = smtplib.SMTP_SSL(SMTP_SERVER, SMTP_PORT, timeout=15)
    server.login(SMTP_USER, SMTP_PASS)
    server.sendmail(FROM_EMAIL, [TO_EMAIL], msg.as_string())
    server.quit()

    print("邮件发送成功:", subject)


# ==================================================
# 主逻辑
# ==================================================
def main():
    while True:
        try:
            sent_ids = load_sent_ids()
            rows = query_level4_alerts()

            for row in rows:
                alert_id = str(row[0])

                if alert_id in sent_ids:
                    continue

                send_mail(row)
                save_sent_id(alert_id)

        except Exception as e:
            print(e)

        time.sleep(600)   # 10分钟

if __name__ == "__main__":
    main()
