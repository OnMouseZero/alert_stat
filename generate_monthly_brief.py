import argparse
import base64
import datetime
import json
import logging
import os
import sqlite3
from collections import defaultdict
from io import BytesIO


DB_FILE = "alerts.db"
TABLE_NAME = "weekly_alerts"
DEFAULT_CONFIG_FILE = "monthly_report_config.json"
DEFAULT_REPORTER = "数据专班运维组"
DEFAULT_TOP_N = 10

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(message)s")


def get_table_columns(cursor, table_name):
    cursor.execute(f"PRAGMA table_info({table_name})")
    return {row[1] for row in cursor.fetchall()}


def get_matplotlib_pyplot():
    try:
        os.makedirs(".matplotlib", exist_ok=True)
        os.environ.setdefault("MPLCONFIGDIR", os.path.abspath(".matplotlib"))
        os.environ.setdefault("MPLBACKEND", "Agg")
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ModuleNotFoundError as exc:
        raise ModuleNotFoundError("缺少 matplotlib，请先执行 `pip install -r requirements.txt`。") from exc

    plt.rcParams["font.sans-serif"] = ["SimHei", "WenQuanYi Micro Hei", "DejaVu Sans"]
    plt.rcParams["axes.unicode_minus"] = False
    return plt


def get_weasyprint_html():
    try:
        from weasyprint import HTML
    except ModuleNotFoundError as exc:
        raise ModuleNotFoundError("缺少 WeasyPrint，请先执行 `pip install -r requirements.txt`。") from exc

    return HTML


def get_previous_month():
    today = datetime.datetime.now()
    first_day = today.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    previous_month_end = first_day - datetime.timedelta(seconds=1)
    return previous_month_end.strftime("%Y-%m")


def parse_args():
    parser = argparse.ArgumentParser(description="生成月度告警统计简报")
    parser.add_argument("--month", default=get_previous_month(), help="统计月份，格式如 2026-04")
    parser.add_argument("--exclude-dates", default="", help="排除日期，逗号分隔，例如 2026-04-04,2026-04-05")
    parser.add_argument("--config", default=DEFAULT_CONFIG_FILE, help="月报配置文件路径")
    parser.add_argument("--output-format", choices=["html", "pdf", "both"], default="html", help="输出格式")
    parser.add_argument("--html-file", default="", help="自定义 HTML 输出文件名")
    parser.add_argument("--pdf-file", default="", help="自定义 PDF 输出文件名")
    parser.add_argument("--top-n", type=int, default=DEFAULT_TOP_N, help="高频告警展示条数")
    parser.add_argument("--stale-days", type=int, default=None, help="长期未清零判定阈值，单位天")
    return parser.parse_args()


def normalize_month(month_text):
    normalized = month_text.strip().replace(".", "-").replace("/", "-")
    return datetime.datetime.strptime(normalized, "%Y-%m")


def get_month_range(month_text):
    month_dt = normalize_month(month_text)
    start_dt = month_dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    next_month = (start_dt.replace(day=28) + datetime.timedelta(days=4)).replace(day=1)
    end_dt = next_month - datetime.timedelta(seconds=1)
    return start_dt, end_dt


def load_config(config_path):
    if not config_path:
        return {}

    if os.path.exists(config_path):
        with open(config_path, "r", encoding="utf-8") as file_obj:
            return json.load(file_obj)

    if config_path == DEFAULT_CONFIG_FILE:
        return {}

    raise FileNotFoundError(f"配置文件不存在: {config_path}")


def normalize_date(date_text):
    return date_text.strip().replace(".", "-").replace("/", "-")


def parse_exclude_dates(cli_text, config_data, start_dt, end_dt):
    date_values = set()
    raw_values = []

    if cli_text:
        raw_values.extend(item for item in cli_text.split(",") if item.strip())

    raw_values.extend(config_data.get("exclude_dates", []))

    normalized_values = []
    for raw_value in raw_values:
        candidate = normalize_date(str(raw_value))
        try:
            parsed = datetime.datetime.strptime(candidate, "%Y-%m-%d")
        except ValueError:
            logging.warning(f"忽略无效排除日期: {raw_value}")
            continue

        if not (start_dt.date() <= parsed.date() <= end_dt.date()):
            logging.warning(f"忽略不在统计月份内的排除日期: {candidate}")
            continue

        date_values.add(candidate)

    normalized_values.extend(sorted(date_values))
    return normalized_values


def build_exclude_clause(column_name, exclude_dates):
    if not exclude_dates:
        return "", []

    placeholders = ",".join("?" for _ in exclude_dates)
    return f" AND substr({column_name}, 1, 10) NOT IN ({placeholders})", exclude_dates


def fetch_monthly_report(month_text, exclude_dates, top_n, stale_days):
    if not os.path.exists(DB_FILE):
        raise FileNotFoundError(f"数据库文件不存在: {DB_FILE}")

    start_dt, end_dt = get_month_range(month_text)
    start_fmt = start_dt.strftime("%Y-%m-%d %H:%M:%S")
    end_fmt = end_dt.strftime("%Y-%m-%d %H:%M:%S")
    exclude_sql, exclude_params = build_exclude_clause("t1.created_at", exclude_dates)

    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()

    try:
        table_columns = get_table_columns(cursor, TABLE_NAME)
        has_first_status = "first_status" in table_columns
        has_status = "status" in table_columns
        has_resolved_at = "resolved_at" in table_columns
        recovery_metrics_available = has_status and has_resolved_at

        first_status_filter = "COALESCE(t1.first_status, 'firing') = 'firing'" if has_first_status else "1=1"
        first_status_filter_plain = "COALESCE(first_status, 'firing') = 'firing'" if has_first_status else "1=1"
        current_resolved_expr = "COALESCE(t1.status, 'firing') = 'resolved'" if has_status else "0=1"
        current_unresolved_expr = "COALESCE(t1.status, 'firing') != 'resolved'" if has_status else "1=1"
        current_resolved_expr_plain = "COALESCE(status, 'firing') = 'resolved'" if has_status else "0=1"
        current_unresolved_expr_plain = "COALESCE(status, 'firing') != 'resolved'" if has_status else "1=1"
        resolved_by_month_end_expr = (
            "t1.resolved_at IS NOT NULL AND t1.resolved_at != '' AND t1.resolved_at <= ?"
            if has_resolved_at else
            "0=1"
        )
        unresolved_by_month_end_expr = (
            "t1.resolved_at IS NULL OR t1.resolved_at = '' OR t1.resolved_at > ?"
            if has_resolved_at else
            "1=1"
        )
        resolved_by_month_end_expr_plain = (
            "resolved_at IS NOT NULL AND resolved_at != '' AND resolved_at <= ?"
            if has_resolved_at else
            "0=1"
        )
        unresolved_by_month_end_expr_plain = (
            "resolved_at IS NULL OR resolved_at = '' OR resolved_at > ?"
            if has_resolved_at else
            "1=1"
        )
        if recovery_metrics_available:
            detail_recovery_select = f"""
            SUM(CASE
                    WHEN {resolved_by_month_end_expr}
                    THEN 1 ELSE 0
                END) as resolved_by_month_end_count,
            SUM(CASE
                    WHEN {unresolved_by_month_end_expr}
                    THEN 1 ELSE 0
                END) as unresolved_by_month_end_count,
            SUM(CASE WHEN {current_resolved_expr} THEN 1 ELSE 0 END) as current_resolved_count,
            SUM(CASE WHEN {current_unresolved_expr} THEN 1 ELSE 0 END) as current_unresolved_count
            """
            summary_recovery_select = f"""
            SUM(CASE
                    WHEN {resolved_by_month_end_expr_plain}
                    THEN 1 ELSE 0
                END) as resolved_by_month_end_total,
            SUM(CASE
                    WHEN {unresolved_by_month_end_expr_plain}
                    THEN 1 ELSE 0
                END) as unresolved_by_month_end_total,
            SUM(CASE WHEN {current_resolved_expr_plain} THEN 1 ELSE 0 END) as current_resolved_total,
            SUM(CASE WHEN {current_unresolved_expr_plain} THEN 1 ELSE 0 END) as current_unresolved_total,
            AVG(CASE
                    WHEN {resolved_by_month_end_expr_plain}
                     AND starts_at IS NOT NULL AND starts_at != ''
                    THEN (julianday(resolved_at) - julianday(starts_at)) * 24
                    ELSE NULL
                END) as avg_recovery_hours_by_month_end,
            """
        else:
            detail_recovery_select = """
            NULL as resolved_by_month_end_count,
            NULL as unresolved_by_month_end_count,
            NULL as current_resolved_count,
            NULL as current_unresolved_count
            """
            summary_recovery_select = """
            NULL as resolved_by_month_end_total,
            NULL as unresolved_by_month_end_total,
            NULL as current_resolved_total,
            NULL as current_unresolved_total,
            NULL as avg_recovery_hours_by_month_end,
            """

        detail_sql = f"""
        SELECT
            t1.cluster,
            t1.namespace,
            t1.alert_name,
            MAX(t1.level) as max_level,
            MAX(t1.metric_type),
            t1.target,
            (SELECT key_info
               FROM {TABLE_NAME} t2
              WHERE t2.cluster = t1.cluster
                AND t2.namespace = t1.namespace
                AND t2.alert_name = t1.alert_name
                AND t2.target = t1.target
              ORDER BY t2.starts_at DESC
               LIMIT 1),
            COUNT(*) as frequency,
            MIN(t1.starts_at),
            MAX(t1.starts_at),
            {detail_recovery_select}
        FROM {TABLE_NAME} t1
        WHERE t1.created_at BETWEEN ? AND ?
          AND {first_status_filter}
          {exclude_sql}
        GROUP BY t1.cluster, t1.namespace, t1.alert_name, t1.target
        ORDER BY frequency DESC, max_level DESC, t1.cluster ASC
        """
        detail_params = []
        if has_resolved_at:
            detail_params.extend([end_fmt, end_fmt])
        detail_params.extend([start_fmt, end_fmt])
        detail_params.extend(exclude_params)
        cursor.execute(detail_sql, detail_params)
        detail_rows = cursor.fetchall()

        trend_sql = f"""
        SELECT strftime('%d', created_at) as day, COUNT(*)
        FROM {TABLE_NAME}
        WHERE created_at BETWEEN ? AND ?
          AND {first_status_filter_plain}
          {exclude_sql.replace('t1.', '')}
        GROUP BY day
        ORDER BY day
        """
        cursor.execute(trend_sql, [start_fmt, end_fmt] + exclude_params)
        trend_data = dict(cursor.fetchall())

        summary_sql = f"""
        SELECT
            COUNT(*) as triggered_total,
            {summary_recovery_select}
            COUNT(DISTINCT cluster) as active_systems
        FROM {TABLE_NAME}
        WHERE created_at BETWEEN ? AND ?
          AND {first_status_filter_plain}
          {exclude_sql.replace('t1.', '')}
        """
        summary_params = []
        if recovery_metrics_available:
            summary_params.extend([end_fmt, end_fmt, end_fmt])
        summary_params.extend([start_fmt, end_fmt])
        summary_params.extend(exclude_params)
        cursor.execute(summary_sql, summary_params)
        summary_row = cursor.fetchone() or (0, 0, 0, 0, 0, None, 0)

        unresolved_sql = f"""
        SELECT cluster, alert_name, target, level, starts_at
        FROM {TABLE_NAME}
        WHERE created_at BETWEEN ? AND ?
          AND {first_status_filter_plain}
          AND {current_unresolved_expr_plain}
          {exclude_sql.replace('t1.', '')}
        ORDER BY level DESC, created_at DESC
        LIMIT 10
        """
        unresolved_rows = []
        if recovery_metrics_available:
            cursor.execute(unresolved_sql, [start_fmt, end_fmt] + exclude_params)
            unresolved_rows = cursor.fetchall()

        stale_rows = []
        if recovery_metrics_available:
            stale_sql = f"""
            SELECT
                cluster,
                alert_name,
                target,
                level,
                starts_at,
                CAST(julianday(?) - julianday(starts_at) AS INTEGER) as aging_days,
                key_info
            FROM {TABLE_NAME}
            WHERE {first_status_filter_plain}
              AND starts_at <= ?
              AND ({unresolved_by_month_end_expr_plain})
              AND CAST(julianday(?) - julianday(starts_at) AS INTEGER) >= ?
              {exclude_sql.replace('t1.', '')}
            ORDER BY aging_days DESC, level DESC, starts_at ASC
            LIMIT 20
            """
            cursor.execute(stale_sql, [end_fmt, end_fmt, end_fmt, end_fmt, stale_days] + exclude_params)
            stale_rows = cursor.fetchall()

        summary = {
            "triggered_total": summary_row[0] or 0,
            "resolved_by_month_end_total": summary_row[1] if recovery_metrics_available else None,
            "unresolved_by_month_end_total": summary_row[2] if recovery_metrics_available else None,
            "current_resolved_total": summary_row[3] if recovery_metrics_available else None,
            "current_unresolved_total": summary_row[4] if recovery_metrics_available else None,
            "avg_recovery_hours_by_month_end": round(summary_row[5], 2) if recovery_metrics_available and summary_row[5] is not None else None,
            "active_systems": summary_row[6] or 0,
            "recovery_metrics_available": recovery_metrics_available,
        }

        systems_data = defaultdict(
            lambda: {
                "total": 0,
                "resolved_by_month_end": 0,
                "unresolved_by_month_end": 0,
                "current_resolved": 0,
                "current_unresolved": 0,
                "levels": {4: 0, 3: 0, 2: 0, 1: 0},
                "rows": [],
            }
        )
        level_totals = {4: 0, 3: 0, 2: 0, 1: 0}

        for row in detail_rows:
            cluster = row[0]
            frequency = row[7]
            resolved_count = row[10]
            unresolved_count = row[11]
            current_resolved_count = row[12]
            current_unresolved_count = row[13]
            try:
                level = int(row[3])
            except (TypeError, ValueError):
                level = 1

            systems_data[cluster]["rows"].append(row)
            systems_data[cluster]["total"] += frequency
            systems_data[cluster]["resolved_by_month_end"] += resolved_count or 0
            systems_data[cluster]["unresolved_by_month_end"] += unresolved_count or 0
            systems_data[cluster]["current_resolved"] += current_resolved_count or 0
            systems_data[cluster]["current_unresolved"] += current_unresolved_count or 0
            systems_data[cluster]["levels"][level] += frequency
            level_totals[level] += frequency

        return {
            "month_text": month_text,
            "start_dt": start_dt,
            "end_dt": end_dt,
            "summary": summary,
            "trend_data": trend_data,
            "detail_rows": detail_rows,
            "systems_data": systems_data,
            "level_totals": level_totals,
            "unresolved_rows": unresolved_rows,
            "stale_rows": stale_rows,
            "top_rows": detail_rows[:top_n],
            "exclude_dates": exclude_dates,
            "stale_days": stale_days,
        }
    finally:
        conn.close()


def generate_trend_chart(trend_data, start_dt, end_dt):
    plt = get_matplotlib_pyplot()
    days = []
    counts = []
    current_dt = start_dt

    while current_dt <= end_dt:
        day_text = current_dt.strftime("%d")
        days.append(day_text)
        counts.append(trend_data.get(day_text, 0))
        current_dt += datetime.timedelta(days=1)

    positions = list(range(len(days)))
    plt.figure(figsize=(12, 3.8))
    bars = plt.bar(positions, counts, color="#1f6aa5", alpha=0.78, width=0.62)
    plt.plot(positions, counts, marker="o", color="#d45d00", linewidth=2, markersize=4)
    plt.xticks(positions, days)
    plt.title("月度告警趋势图", fontsize=12, pad=10)
    plt.grid(axis="y", linestyle="--", alpha=0.28)

    for bar in bars:
        height = bar.get_height()
        if height > 0:
            plt.text(
                bar.get_x() + bar.get_width() / 2.0,
                height + 0.2,
                f"{int(height)}",
                ha="center",
                va="bottom",
                fontsize=8,
            )

    buffer = BytesIO()
    plt.savefig(buffer, format="png", bbox_inches="tight", dpi=110)
    plt.close()
    return base64.b64encode(buffer.getvalue()).decode("utf-8")


def build_overview_sentence(report_data):
    summary = report_data["summary"]
    start_dt = report_data["start_dt"]
    end_dt = report_data["end_dt"]

    if not summary.get("recovery_metrics_available", False):
        return (
            f"{start_dt.month}月{start_dt.day}日至{end_dt.month}月{end_dt.day}日，"
            f"监控系统累计产生告警 {summary['triggered_total']} 次。"
            "由于当前数据库仍是旧结构，历史恢复数据未入库，本月恢复率和未清零情况暂无法准确计算。"
        )

    recovery_rate = 0
    if summary["triggered_total"]:
        recovery_rate = round(summary["resolved_by_month_end_total"] / summary["triggered_total"] * 100, 2)

    return (
        f"{start_dt.month}月{start_dt.day}日至{end_dt.month}月{end_dt.day}日，"
        f"监控系统累计产生告警 {summary['triggered_total']} 次，"
        f"截至月末已恢复 {summary['resolved_by_month_end_total']} 次，"
        f"月末恢复率 {recovery_rate}%；"
        f"月末仍有 {summary['unresolved_by_month_end_total']} 次未恢复告警。"
    )


def build_auto_follow_up_notes(report_data):
    notes = []
    summary = report_data["summary"]
    top_rows = report_data["top_rows"]
    unresolved_rows = report_data["unresolved_rows"]
    stale_rows = report_data["stale_rows"]

    if top_rows:
        top_item = top_rows[0]
        notes.append(
            f"高频告警主要集中在“{top_item[0]} / {top_item[2]}”，本月共触发 {top_item[7]} 次，建议优先检查该类阈值和资源瓶颈。"
        )

    if not summary.get("recovery_metrics_available", False):
        notes.append("当前历史库缺少恢复生命周期字段，恢复率、长期未清零告警只能从新接收逻辑启用后开始准确统计。")

    if summary["unresolved_by_month_end_total"]:
        notes.append(
            f"截至月末仍有 {summary['unresolved_by_month_end_total']} 次告警未恢复，建议对月末未清零告警建立专门跟踪清单，避免跨月遗留。"
        )

    if stale_rows:
        oldest_stale = stale_rows[0]
        notes.append(
            f"存在持续超过 {report_data['stale_days']} 天仍未清零的告警，例如“{oldest_stale[0]} / {oldest_stale[1]} / {oldest_stale[2]}”，需要明确责任人和清零时限。"
        )

    if unresolved_rows:
        first_unresolved = unresolved_rows[0]
        notes.append(
            f"当前最需要关注的未恢复项包括“{first_unresolved[0]} / {first_unresolved[1]} / {first_unresolved[2]}”，建议联系对应系统负责人确认处理时点。"
        )

    if report_data["exclude_dates"]:
        notes.append(
            f"本月报已按要求排除了以下日期的数据：{', '.join(report_data['exclude_dates'])}。如后续复盘需要，请同步说明排除原因。"
        )

    if not notes:
        notes.append("本月未查询到告警数据，无需额外跟进事项。")

    return notes


def build_system_inventory_from_report(report_data, top_n=3):
    systems_data = report_data["systems_data"]
    ranked_systems = sorted(
        systems_data.items(),
        key=lambda item: item[1]["total"],
        reverse=True,
    )
    return [
        {"system": cluster, "count": data["total"]}
        for cluster, data in ranked_systems[:top_n]
    ]


def generate_html(report_data, config_data, top_n):
    summary = report_data["summary"]
    systems_data = report_data["systems_data"]
    top_rows = report_data["top_rows"]
    unresolved_rows = report_data["unresolved_rows"]
    stale_rows = report_data["stale_rows"]
    exclude_dates = report_data["exclude_dates"]
    month_dt = normalize_month(report_data["month_text"])
    stale_days = report_data["stale_days"]

    title = config_data.get("report_title") or f"{month_dt.year}年{month_dt.month}月份监控告警信息统计简报"
    reporter = config_data.get("reporter", DEFAULT_REPORTER)
    generated_date = config_data.get("generated_date", datetime.datetime.now().strftime("%Y-%m-%d"))
    overview_points = config_data.get("overview_points", [])
    system_inventory = build_system_inventory_from_report(report_data, top_n=3)
    analysis_responses = config_data.get("analysis_responses", [])
    follow_up_notes = config_data.get("follow_up_notes") or build_auto_follow_up_notes(report_data)
    trend_chart = generate_trend_chart(report_data["trend_data"], report_data["start_dt"], report_data["end_dt"])

    month_end_recovery_rate = 0
    if summary["triggered_total"] and summary.get("recovery_metrics_available", False):
        month_end_recovery_rate = round(summary["resolved_by_month_end_total"] / summary["triggered_total"] * 100, 2)

    current_recovery_rate = 0
    if summary["triggered_total"] and summary.get("recovery_metrics_available", False):
        current_recovery_rate = round(summary["current_resolved_total"] / summary["triggered_total"] * 100, 2)

    level_map = {4: "紧急", 3: "严重", 2: "中度", 1: "轻微"}
    overview_sentence = build_overview_sentence(report_data)
    resolved_by_month_end_text = summary["resolved_by_month_end_total"] if summary["resolved_by_month_end_total"] is not None else "-"
    unresolved_by_month_end_text = summary["unresolved_by_month_end_total"] if summary["unresolved_by_month_end_total"] is not None else "-"
    current_unresolved_text = summary["current_unresolved_total"] if summary["current_unresolved_total"] is not None else "-"
    avg_recovery_text = summary["avg_recovery_hours_by_month_end"] if summary["avg_recovery_hours_by_month_end"] is not None else "-"
    month_end_recovery_rate_text = f"{month_end_recovery_rate}%" if summary.get("recovery_metrics_available", False) else "-"
    current_recovery_rate_text = f"{current_recovery_rate}%" if summary.get("recovery_metrics_available", False) else "-"

    css = """
    <style>
        @page { margin: 1.2cm; size: A4; }
        body { font-family: "WenQuanYi Micro Hei", sans-serif; font-size: 11px; color: #22313f; line-height: 1.55; background: #f5f1e8; }
        .page { background: linear-gradient(180deg, #f8f4ec 0%, #fbfaf6 100%); padding: 18px 22px 24px 22px; border: 1px solid #d7c9b3; }
        .header { border-bottom: 3px solid #8f4b22; padding-bottom: 12px; margin-bottom: 18px; position: relative; }
        .title { font-size: 24px; font-weight: bold; color: #6b2d14; letter-spacing: 1px; }
        .subtitle { color: #7d6b57; margin-top: 6px; }
        .badge-box { position: absolute; top: 0; right: 0; text-align: right; font-size: 10px; color: #735b47; }
        .section { margin-bottom: 22px; }
        .section-title { font-size: 16px; font-weight: bold; color: #6b2d14; border-left: 5px solid #b5651d; padding-left: 10px; margin-bottom: 10px; }
        .overview-box { background: #fffdf8; border: 1px solid #e4d7c3; border-radius: 8px; padding: 12px 14px; margin-bottom: 14px; }
        .overview-box ul { margin: 6px 0 0 18px; padding: 0; }
        .overview-box li { margin-bottom: 4px; }
        .stats-grid { display: flex; gap: 10px; margin-bottom: 16px; }
        .stats-card { flex: 1; background: #fff; border: 1px solid #e4d7c3; border-radius: 8px; padding: 10px 12px; }
        .stats-card .label { font-size: 10px; color: #7d6b57; margin-bottom: 6px; }
        .stats-card .value { font-size: 20px; font-weight: bold; }
        .value-red { color: #b33a3a; }
        .value-green { color: #2f7d4a; }
        .value-blue { color: #1f6aa5; }
        .value-brown { color: #6b2d14; }
        .minor-note { font-size: 10px; color: #7d6b57; margin-top: 4px; }
        .chart-box { background: #fff; border: 1px solid #e4d7c3; border-radius: 8px; padding: 10px; margin-bottom: 16px; }
        .inventory-table, .data-table { width: 100%; border-collapse: collapse; background: #fff; }
        .inventory-table th, .inventory-table td, .data-table th, .data-table td { border: 1px solid #e5dbc9; padding: 6px 8px; }
        .inventory-table th, .data-table th { background: #efe2cf; color: #69482a; }
        .muted { color: #7d6b57; }
        .pill { display: inline-block; border-radius: 999px; padding: 2px 8px; font-size: 10px; color: #fff; }
        .pill-4 { background: #b33a3a; }
        .pill-3 { background: #db6b2b; }
        .pill-2 { background: #ba8d18; color: #fff; }
        .pill-1 { background: #2a7d91; }
        .two-col { display: flex; gap: 14px; }
        .col { flex: 1; }
        .system-box { background: #fff; border: 1px solid #e4d7c3; border-radius: 8px; padding: 10px 12px; margin-bottom: 12px; }
        .system-name { font-size: 14px; font-weight: bold; color: #6b2d14; margin-bottom: 6px; }
        .footer { margin-top: 20px; font-size: 11px; color: #6c5c49; display: flex; justify-content: space-between; }
        .exclude-note { margin-top: 8px; color: #8a5a1f; font-size: 10px; }
        .empty-box { background: #fff; border: 1px dashed #d0c0a9; padding: 12px; color: #7d6b57; }
    </style>
    """

    html_parts = [
        f"<html><head><meta charset='UTF-8'>{css}</head><body><div class='page'>",
        "<div class='header'>",
        f"<div class='title'>{title}</div>",
        f"<div class='subtitle'>统计周期：{report_data['start_dt'].strftime('%Y-%m-%d')} 至 {report_data['end_dt'].strftime('%Y-%m-%d')}</div>",
        "<div class='badge-box'>",
        f"自动生成时间：{generated_date}<br>本月产生告警系统：<b>{summary['active_systems']}</b> 个",
        "</div></div>",
        "<div class='section'>",
        "<div class='section-title'>一、整体告警概况</div>",
        "<div class='overview-box'>",
        f"<div>{overview_sentence}</div>",
    ]

    if overview_points:
        html_parts.append("<ul>")
        for item in overview_points:
            html_parts.append(f"<li>{item}</li>")
        html_parts.append("</ul>")

    if exclude_dates:
        html_parts.append(f"<div class='exclude-note'>本次统计已排除日期：{', '.join(exclude_dates)}</div>")

    html_parts.append("</div>")
    html_parts.append("<div class='stats-grid'>")
    html_parts.append(
        f"<div class='stats-card'><div class='label'>本月触发告警总数</div><div class='value value-red'>{summary['triggered_total']}</div></div>"
    )
    html_parts.append(
        f"<div class='stats-card'><div class='label'>截至月末已恢复</div><div class='value value-green'>{resolved_by_month_end_text}</div><div class='minor-note'>月末恢复率 {month_end_recovery_rate_text}</div></div>"
    )
    html_parts.append(
        f"<div class='stats-card'><div class='label'>截至月末未恢复</div><div class='value value-brown'>{unresolved_by_month_end_text}</div><div class='minor-note'>当前未恢复 {current_unresolved_text}</div></div>"
    )
    html_parts.append(
        f"<div class='stats-card'><div class='label'>平均恢复时长</div><div class='value value-blue'>{avg_recovery_text}</div><div class='minor-note'>当前恢复率 {current_recovery_rate_text}</div></div>"
    )
    html_parts.append("</div>")

    if not summary.get("recovery_metrics_available", False):
        html_parts.append(
            "<div class='overview-box' style='border-color:#d8c39b; color:#7d5b24;'>"
            "提示：当前数据库仍是旧结构，仅保存触发告警，未保存恢复生命周期数据。"
            "因此本月恢复率、月末未清零、长期未清零清单暂不具备准确统计基础。"
            "</div>"
        )

    html_parts.append("<div class='chart-box'>")
    html_parts.append("<div style='font-weight:bold; margin-bottom:6px; color:#69482a;'>月度告警趋势</div>")
    html_parts.append(f"<img src='data:image/png;base64,{trend_chart}' style='width:100%;'>")
    html_parts.append("</div>")

    if system_inventory:
        html_parts.append("<div style='font-weight:bold; margin-bottom:6px; color:#69482a;'>服务器监控总览</div>")
        html_parts.append("<table class='inventory-table'><thead><tr><th>系统名称</th><th>数量</th></tr></thead><tbody>")
        for item in system_inventory:
            html_parts.append(
                f"<tr><td>{item.get('system', '')}</td><td>{item.get('count', '')}</td></tr>"
            )
        html_parts.append("</tbody></table>")

    html_parts.append("</div>")

    html_parts.append("<div class='section'>")
    html_parts.append("<div class='section-title'>二、高频告警的系统信息统计</div>")
    if top_rows:
        html_parts.append(
            "<table class='data-table'><thead><tr><th>系统</th><th>告警级别</th><th>次数</th><th>月末已恢复</th><th>月末未恢复</th><th>恢复率</th><th>核心告警内容</th></tr></thead><tbody>"
        )
        for row in top_rows:
            level_num = int(row[3]) if str(row[3]).isdigit() else 1
            row_recovery_rate = round(row[10] / row[7] * 100, 2) if row[7] and summary.get("recovery_metrics_available", False) else "-"
            resolved_count_text = row[10] if row[10] is not None else "-"
            unresolved_count_text = row[11] if row[11] is not None else "-"
            html_parts.append(
                "<tr>"
                f"<td>{row[0]}</td>"
                f"<td><span class='pill pill-{level_num}'>{level_map.get(level_num, '未知')}</span></td>"
                f"<td>{row[7]}</td>"
                f"<td>{resolved_count_text}</td>"
                f"<td>{unresolved_count_text}</td>"
                f"<td>{str(row_recovery_rate) + '%' if row_recovery_rate != '-' else '-'}</td>"
                f"<td>{row[6] or row[2]}</td>"
                "</tr>"
            )
        html_parts.append("</tbody></table>")
        html_parts.append(
            f"<div class='minor-note'>当前展示 Top {min(top_n, len(top_rows))} 条高频告警。</div>"
        )
    else:
        html_parts.append("<div class='empty-box'>本月无告警数据。</div>")
    html_parts.append("</div>")

    html_parts.append("<div class='section'>")
    html_parts.append(f"<div class='section-title'>三、长期未恢复/未清零告警（持续至少 {stale_days} 天）</div>")
    if not summary.get("recovery_metrics_available", False):
        html_parts.append("<div class='empty-box'>旧库没有恢复数据，暂时无法识别长期未清零告警。</div>")
    elif stale_rows:
        html_parts.append(
            "<table class='data-table'><thead><tr><th>系统名称</th><th>告警项</th><th>对象</th><th>级别</th><th>开始时间</th><th>截至月末持续天数</th><th>核心信息</th></tr></thead><tbody>"
        )
        for row in stale_rows:
            level_num = int(row[3]) if str(row[3]).isdigit() else 1
            html_parts.append(
                "<tr>"
                f"<td>{row[0]}</td>"
                f"<td>{row[1]}</td>"
                f"<td>{row[2]}</td>"
                f"<td><span class='pill pill-{level_num}'>{level_map.get(level_num, '未知')}</span></td>"
                f"<td>{str(row[4])[:16]}</td>"
                f"<td>{row[5]}</td>"
                f"<td>{row[6] or ''}</td>"
                "</tr>"
            )
        html_parts.append("</tbody></table>")
    else:
        html_parts.append("<div class='empty-box'>截至月末没有超过阈值天数仍未清零的告警。</div>")
    html_parts.append("</div>")

    html_parts.append("<div class='section'>")
    html_parts.append("<div class='section-title'>四、分系统告警明细</div>")
    if systems_data:
        for cluster, data in sorted(systems_data.items(), key=lambda item: item[1]["total"], reverse=True):
            html_parts.append("<div class='system-box'>")
            html_parts.append(f"<div class='system-name'>{cluster}</div>")
            resolved_system_text = data['resolved_by_month_end'] if summary.get("recovery_metrics_available", False) else "-"
            unresolved_system_text = data['unresolved_by_month_end'] if summary.get("recovery_metrics_available", False) else "-"
            current_unresolved_system_text = data['current_unresolved'] if summary.get("recovery_metrics_available", False) else "-"
            html_parts.append(
                f"<div class='muted'>告警总数：<b>{data['total']}</b>，月末已恢复：<b>{resolved_system_text}</b>，月末未恢复：<b>{unresolved_system_text}</b></div>"
            )
            html_parts.append(
                f"<div class='minor-note'>级别分布：紧急(4) {data['levels'][4]} / 严重(3) {data['levels'][3]} / 中度(2) {data['levels'][2]} / 轻微(1) {data['levels'][1]}；当前未恢复 {current_unresolved_system_text}</div>"
            )
            html_parts.append(
                "<table class='data-table' style='margin-top:8px;'><thead><tr><th>告警名称</th><th>对象</th><th>级别</th><th>次数</th><th>月末已恢复</th><th>月末未恢复</th><th>恢复率</th><th>最近发生</th></tr></thead><tbody>"
            )
            for row in data["rows"][:8]:
                level_num = int(row[3]) if str(row[3]).isdigit() else 1
                row_recovery_rate = round(row[10] / row[7] * 100, 2) if row[7] and summary.get("recovery_metrics_available", False) else "-"
                resolved_count_text = row[10] if row[10] is not None else "-"
                unresolved_count_text = row[11] if row[11] is not None else "-"
                html_parts.append(
                    "<tr>"
                    f"<td>{row[2]}</td>"
                    f"<td>{row[5]}</td>"
                    f"<td><span class='pill pill-{level_num}'>{level_map.get(level_num, '未知')}</span></td>"
                    f"<td>{row[7]}</td>"
                    f"<td>{resolved_count_text}</td>"
                    f"<td>{unresolved_count_text}</td>"
                    f"<td>{str(row_recovery_rate) + '%' if row_recovery_rate != '-' else '-'}</td>"
                    f"<td>{str(row[9])[:16]}</td>"
                    "</tr>"
                )
            html_parts.append("</tbody></table></div>")
    else:
        html_parts.append("<div class='empty-box'>本月无分系统明细数据。</div>")
    html_parts.append("</div>")

    html_parts.append("<div class='section'>")
    html_parts.append("<div class='section-title'>五、告警分析及响应</div>")
    if analysis_responses:
        html_parts.append(
            "<table class='data-table'><thead><tr><th>系统名称</th><th>告警项</th><th>系统联系人</th><th>告警项回复</th><th>回复时间</th></tr></thead><tbody>"
        )
        for item in analysis_responses:
            html_parts.append(
                "<tr>"
                f"<td>{item.get('system', '')}</td>"
                f"<td>{item.get('alert_item', '')}</td>"
                f"<td>{item.get('owner', '')}</td>"
                f"<td>{item.get('response', '')}</td>"
                f"<td>{item.get('response_time', '')}</td>"
                "</tr>"
            )
        html_parts.append("</tbody></table>")
    elif unresolved_rows:
        html_parts.append(
            "<table class='data-table'><thead><tr><th>系统名称</th><th>告警项</th><th>对象</th><th>级别</th><th>最近开始时间</th><th>建议动作</th></tr></thead><tbody>"
        )
        for row in unresolved_rows:
            level_num = int(row[3]) if str(row[3]).isdigit() else 1
            html_parts.append(
                "<tr>"
                f"<td>{row[0]}</td>"
                f"<td>{row[1]}</td>"
                f"<td>{row[2]}</td>"
                f"<td><span class='pill pill-{level_num}'>{level_map.get(level_num, '未知')}</span></td>"
                f"<td>{str(row[4])[:16]}</td>"
                "<td>建议联系系统负责人确认是否为资源瓶颈、阈值偏严或计划内操作。</td>"
                "</tr>"
            )
        html_parts.append("</tbody></table>")
    else:
        html_parts.append("<div class='empty-box'>当前未配置人工分析表，且本月无未恢复告警。</div>")
    html_parts.append("</div>")

    html_parts.append("<div class='section'>")
    html_parts.append("<div class='section-title'>六、关键问题与后续告警处理</div>")
    html_parts.append("<div class='overview-box'><ol>")
    for item in follow_up_notes:
        html_parts.append(f"<li>{item}</li>")
    html_parts.append("</ol></div></div>")

    html_parts.append(
        f"<div class='footer'><div>汇报人：{reporter}</div><div>日期：{generated_date}</div></div>"
    )
    html_parts.append("</div></body></html>")
    return "".join(html_parts)


def write_outputs(html, args):
    safe_month = normalize_month(args.month).strftime("%Y%m")
    html_file = args.html_file or f"monthly_alert_brief_{safe_month}.html"
    pdf_file = args.pdf_file or f"monthly_alert_brief_{safe_month}.pdf"

    if args.output_format in {"html", "both"}:
        with open(html_file, "w", encoding="utf-8") as file_obj:
            file_obj.write(html)
        print(f"HTML 已生成: {html_file}")

    if args.output_format in {"pdf", "both"}:
        HTML = get_weasyprint_html()
        HTML(string=html).write_pdf(pdf_file)
        print(f"PDF 已生成: {pdf_file}")


def main():
    args = parse_args()
    config_data = load_config(args.config)
    start_dt, end_dt = get_month_range(args.month)
    exclude_dates = parse_exclude_dates(args.exclude_dates, config_data, start_dt, end_dt)
    stale_days = args.stale_days if args.stale_days is not None else int(config_data.get("stale_days_threshold", 7))
    report_data = fetch_monthly_report(args.month, exclude_dates, args.top_n, stale_days)
    html = generate_html(report_data, config_data, args.top_n)
    write_outputs(html, args)


if __name__ == "__main__":
    main()
