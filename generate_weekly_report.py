import argparse
import base64
import datetime
import logging
import os
import sqlite3
from collections import defaultdict
from io import BytesIO


DB_FILE = "alerts.db"
TABLE_NAME = "weekly_alerts"
DEFAULT_PDF_FILE = "weekly_inspection_report_custom.pdf"
DEFAULT_HTML_FILE = "weekly_inspection_report_custom.html"
DEFAULT_TOP_N = 10
DEFAULT_STALE_DAYS = 7

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(message)s")


def get_matplotlib_pyplot():
    try:
        os.makedirs(".matplotlib", exist_ok=True)
        os.environ.setdefault("MPLCONFIGDIR", os.path.abspath(".matplotlib"))
        os.environ.setdefault("MPLBACKEND", "Agg")
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ModuleNotFoundError as exc:
        raise ModuleNotFoundError("缺少 matplotlib，请先安装后再生成周报。") from exc

    plt.rcParams["font.sans-serif"] = ["SimHei", "WenQuanYi Micro Hei", "DejaVu Sans"]
    plt.rcParams["axes.unicode_minus"] = False
    return plt


def get_weasyprint_html():
    try:
        from weasyprint import HTML
    except ModuleNotFoundError as exc:
        raise ModuleNotFoundError("缺少 WeasyPrint，请先安装后再导出 PDF。") from exc
    except OSError as exc:
        raise RuntimeError(
            "WeasyPrint 已安装，但系统缺少 GTK/Pango 等底层依赖，当前机器暂时无法直接导出 PDF。"
        ) from exc

    return HTML


def parse_args():
    parser = argparse.ArgumentParser(description="生成周度告警行动简报")
    parser.add_argument("--start", default="", help="开始日期，格式如 2026-05-01")
    parser.add_argument("--end", default="", help="结束日期，格式如 2026-05-07")
    parser.add_argument("--days", type=int, default=7, help="未指定开始/结束日期时，默认统计最近 N 天")
    parser.add_argument("--top-n", type=int, default=DEFAULT_TOP_N, help="Top 清单展示条数")
    parser.add_argument("--stale-days", type=int, default=DEFAULT_STALE_DAYS, help="长期未恢复判定阈值，单位天")
    parser.add_argument("--output-format", choices=["html", "pdf", "both"], default="pdf", help="输出格式")
    parser.add_argument("--html-file", default="", help="自定义 HTML 输出文件名")
    parser.add_argument("--pdf-file", default="", help="自定义 PDF 输出文件名")
    return parser.parse_args()


def normalize_date(date_text):
    return date_text.strip().replace(".", "-").replace("/", "-")


def parse_date(date_text):
    return datetime.datetime.strptime(normalize_date(date_text), "%Y-%m-%d")


def build_date_range(args):
    if args.start and args.end:
        start_dt = parse_date(args.start).replace(hour=0, minute=0, second=0, microsecond=0)
        end_dt = parse_date(args.end).replace(hour=23, minute=59, second=59, microsecond=0)
        return start_dt, end_dt

    if args.start or args.end:
        raise ValueError("开始日期和结束日期需要同时提供。")

    end_dt = datetime.datetime.now().replace(microsecond=0)
    start_dt = (end_dt - datetime.timedelta(days=args.days)).replace(hour=0, minute=0, second=0, microsecond=0)
    return start_dt, end_dt


def get_table_columns(cursor, table_name):
    cursor.execute(f"PRAGMA table_info({table_name})")
    return {row[1] for row in cursor.fetchall()}


def format_percent(numerator, denominator):
    if not denominator:
        return "-"
    return f"{round(numerator / denominator * 100, 1)}%"


def build_trend_chart(trend_data, start_dt, end_dt):
    plt = get_matplotlib_pyplot()
    day_labels = []
    counts = []
    current_dt = start_dt

    while current_dt.date() <= end_dt.date():
        label = current_dt.strftime("%m-%d")
        day_labels.append(label)
        counts.append(trend_data.get(label, 0))
        current_dt += datetime.timedelta(days=1)

    positions = list(range(len(day_labels)))
    average_count = round(sum(counts) / len(counts), 1) if counts else 0

    plt.figure(figsize=(10.5, 3.5))
    bars = plt.bar(positions, counts, color="#b5651d", alpha=0.82, width=0.58)
    plt.plot(positions, counts, color="#244a68", linewidth=2, marker="o", markersize=4)
    plt.axhline(average_count, color="#6c757d", linestyle="--", linewidth=1.2)
    plt.xticks(positions, day_labels)
    plt.title("本周告警趋势", fontsize=12, pad=10)
    plt.grid(axis="y", linestyle="--", alpha=0.25)

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


def fetch_weekly_report(start_dt, end_dt, top_n, stale_days):
    if not os.path.exists(DB_FILE):
        raise FileNotFoundError(f"数据库文件不存在: {DB_FILE}")

    start_fmt = start_dt.strftime("%Y-%m-%d %H:%M:%S")
    end_fmt = end_dt.strftime("%Y-%m-%d %H:%M:%S")

    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()

    try:
        columns = get_table_columns(cursor, TABLE_NAME)
        recovery_metrics_available = {"status", "resolved_at"}.issubset(columns)

        detail_sql = f"""
        SELECT
            t1.cluster,
            t1.namespace,
            t1.alert_name,
            MAX(CAST(COALESCE(t1.level, '0') AS INTEGER)) as max_level,
            MAX(t1.metric_type) as metric_type,
            t1.target,
            (SELECT key_info
               FROM {TABLE_NAME} t2
              WHERE t2.cluster = t1.cluster
                AND t2.namespace = t1.namespace
                AND t2.alert_name = t1.alert_name
                AND t2.target = t1.target
              ORDER BY t2.starts_at DESC
              LIMIT 1) as key_info,
            (SELECT detail_info
               FROM {TABLE_NAME} t3
              WHERE t3.cluster = t1.cluster
                AND t3.namespace = t1.namespace
                AND t3.alert_name = t1.alert_name
                AND t3.target = t1.target
              ORDER BY t3.starts_at DESC
              LIMIT 1) as detail_info,
            COUNT(*) as frequency,
            MIN(t1.starts_at) as first_seen,
            MAX(t1.starts_at) as last_seen,
            MAX(t1.created_at) as last_created_at,
            SUM(CASE WHEN CAST(COALESCE(t1.level, '0') AS INTEGER) = 4 THEN 1 ELSE 0 END) as level4_count,
            SUM(CASE WHEN CAST(COALESCE(t1.level, '0') AS INTEGER) >= 3 THEN 1 ELSE 0 END) as level3plus_count
        FROM {TABLE_NAME} t1
        WHERE t1.created_at BETWEEN ? AND ?
        GROUP BY t1.cluster, t1.namespace, t1.alert_name, t1.target
        ORDER BY max_level DESC, frequency DESC, t1.cluster ASC
        """
        cursor.execute(detail_sql, (start_fmt, end_fmt))
        detail_rows = cursor.fetchall()

        trend_sql = f"""
        SELECT strftime('%m-%d', created_at) as day, COUNT(*)
        FROM {TABLE_NAME}
        WHERE created_at BETWEEN ? AND ?
        GROUP BY day
        ORDER BY day
        """
        cursor.execute(trend_sql, (start_fmt, end_fmt))
        trend_data = dict(cursor.fetchall())

        summary_sql = f"""
        SELECT
            COUNT(*) as total_alerts,
            COUNT(DISTINCT cluster) as affected_systems,
            SUM(CASE WHEN CAST(COALESCE(level, '0') AS INTEGER) = 4 THEN 1 ELSE 0 END) as urgent_total,
            SUM(CASE WHEN CAST(COALESCE(level, '0') AS INTEGER) >= 3 THEN 1 ELSE 0 END) as severe_total
        FROM {TABLE_NAME}
        WHERE created_at BETWEEN ? AND ?
        """
        cursor.execute(summary_sql, (start_fmt, end_fmt))
        summary_row = cursor.fetchone() or (0, 0, 0, 0)

        summary = {
            "total_alerts": summary_row[0] or 0,
            "affected_systems": summary_row[1] or 0,
            "urgent_total": summary_row[2] or 0,
            "severe_total": summary_row[3] or 0,
            "recovery_metrics_available": recovery_metrics_available,
        }

        if recovery_metrics_available:
            recovery_sql = f"""
            SELECT
                SUM(CASE
                        WHEN resolved_at IS NOT NULL AND resolved_at != '' AND resolved_at <= ?
                        THEN 1 ELSE 0
                    END) as resolved_by_end,
                SUM(CASE
                        WHEN resolved_at IS NULL OR resolved_at = '' OR resolved_at > ?
                        THEN 1 ELSE 0
                    END) as unresolved_by_end,
                AVG(CASE
                        WHEN resolved_at IS NOT NULL AND resolved_at != '' AND resolved_at <= ?
                         AND starts_at IS NOT NULL AND starts_at != ''
                        THEN (julianday(resolved_at) - julianday(starts_at)) * 24
                        ELSE NULL
                    END) as avg_recovery_hours
            FROM {TABLE_NAME}
            WHERE created_at BETWEEN ? AND ?
            """
            cursor.execute(recovery_sql, (end_fmt, end_fmt, end_fmt, start_fmt, end_fmt))
            recovery_row = cursor.fetchone() or (0, 0, None)
            summary["resolved_by_end"] = recovery_row[0] or 0
            summary["unresolved_by_end"] = recovery_row[1] or 0
            summary["avg_recovery_hours"] = round(recovery_row[2], 2) if recovery_row[2] is not None else None
            summary["recovery_rate"] = format_percent(summary["resolved_by_end"], summary["total_alerts"])
        else:
            summary["resolved_by_end"] = None
            summary["unresolved_by_end"] = None
            summary["avg_recovery_hours"] = None
            summary["recovery_rate"] = "-"

        systems = defaultdict(
            lambda: {
                "total": 0,
                "urgent": 0,
                "severe": 0,
                "rows": [],
            }
        )
        alert_names = defaultdict(int)

        for row in detail_rows:
            cluster = row[0]
            frequency = row[8]
            systems[cluster]["total"] += frequency
            systems[cluster]["urgent"] += row[12]
            systems[cluster]["severe"] += row[13]
            systems[cluster]["rows"].append(row)
            alert_names[row[2]] += frequency

        top_alerts = sorted(detail_rows, key=lambda row: (row[3], row[8]), reverse=True)[:top_n]
        top_systems = sorted(systems.items(), key=lambda item: item[1]["total"], reverse=True)[:top_n]
        urgent_focus = [row for row in detail_rows if row[3] >= 3][:top_n]

        stale_rows = []
        if recovery_metrics_available:
            stale_sql = f"""
            SELECT
                cluster,
                alert_name,
                target,
                CAST(COALESCE(level, '0') AS INTEGER) as level_num,
                starts_at,
                CAST(julianday(?) - julianday(starts_at) AS INTEGER) as aging_days,
                key_info
            FROM {TABLE_NAME}
            WHERE starts_at IS NOT NULL
              AND starts_at != ''
              AND starts_at <= ?
              AND (resolved_at IS NULL OR resolved_at = '' OR resolved_at > ?)
              AND CAST(julianday(?) - julianday(starts_at) AS INTEGER) >= ?
            ORDER BY aging_days DESC, level_num DESC, starts_at ASC
            LIMIT ?
            """
            threshold_start = (end_dt - datetime.timedelta(days=stale_days)).strftime("%Y-%m-%d %H:%M:%S")
            cursor.execute(
                stale_sql,
                (end_fmt, threshold_start, end_fmt, end_fmt, stale_days, top_n),
            )
            stale_rows = cursor.fetchall()

        peak_day = None
        if trend_data:
            peak_day = max(trend_data.items(), key=lambda item: item[1])

        report_data = {
            "start_dt": start_dt,
            "end_dt": end_dt,
            "summary": summary,
            "trend_data": trend_data,
            "detail_rows": detail_rows,
            "top_alerts": top_alerts,
            "top_systems": top_systems,
            "urgent_focus": urgent_focus,
            "stale_rows": stale_rows,
            "systems": systems,
            "peak_day": peak_day,
            "stale_days": stale_days,
        }
        report_data["action_items"] = build_action_items(report_data)
        return report_data
    finally:
        conn.close()


def build_action_items(report_data):
    summary = report_data["summary"]
    top_alerts = report_data["top_alerts"]
    top_systems = report_data["top_systems"]
    urgent_focus = report_data["urgent_focus"]
    stale_rows = report_data["stale_rows"]
    peak_day = report_data["peak_day"]

    items = []

    if urgent_focus:
        top_urgent = urgent_focus[0]
        items.append(
            f"优先跟进等级较高的告警：{top_urgent[0]} / {top_urgent[2]} / {top_urgent[5]}，本周出现 {top_urgent[8]} 次。"
        )

    if top_systems:
        system_name, system_data = top_systems[0]
        items.append(
            f"重点关注高频系统：{system_name}，本周累计 {system_data['total']} 次告警，建议确认是否存在批量阈值偏严或资源瓶颈。"
        )

    if peak_day:
        items.append(
            f"本周峰值出现在 {peak_day[0]}，当天产生 {peak_day[1]} 次告警，建议回看当天是否存在计划变更、批量发布或平台操作。"
        )

    if summary["recovery_metrics_available"] and summary["unresolved_by_end"]:
        items.append(
            f"截至统计结束仍有 {summary['unresolved_by_end']} 条告警未恢复，需要安排责任人逐项确认是否应清零。"
        )

    if stale_rows:
        oldest = stale_rows[0]
        items.append(
            f"存在持续超过 {report_data['stale_days']} 天未恢复的遗留告警：{oldest[0]} / {oldest[1]} / {oldest[2]}，已持续 {oldest[5]} 天。"
        )

    if not items:
        items.append("本周未发现需要特别升级处理的事项。")

    return items


def generate_html(report_data):
    summary = report_data["summary"]
    top_alerts = report_data["top_alerts"]
    top_systems = report_data["top_systems"]
    urgent_focus = report_data["urgent_focus"]
    stale_rows = report_data["stale_rows"]
    chart_img = build_trend_chart(report_data["trend_data"], report_data["start_dt"], report_data["end_dt"])
    level_map = {4: "紧急", 3: "严重", 2: "中度", 1: "轻微"}

    avg_daily = round(summary["total_alerts"] / max(1, len(report_data["trend_data"]) or 1), 1) if summary["total_alerts"] else 0
    recovery_note = (
        f"截至结束日已恢复 {summary['resolved_by_end']} 条，未恢复 {summary['unresolved_by_end']} 条，恢复率 {summary['recovery_rate']}。"
        if summary["recovery_metrics_available"]
        else "当前数据库未保存恢复生命周期数据，本周报先聚焦新增告警热度与重点跟进项。"
    )

    css = """
    <style>
        @page { margin: 1cm; size: A4; }
        body { font-family: "WenQuanYi Micro Hei", sans-serif; font-size: 11px; color: #21303c; background: #f4efe8; }
        .page { padding: 16px 18px 20px 18px; background: linear-gradient(180deg, #f8f3ea 0%, #fcfbf8 100%); border: 1px solid #d8c9b4; }
        .header { display:flex; justify-content:space-between; align-items:flex-start; border-bottom: 3px solid #8f4b22; padding-bottom: 12px; margin-bottom: 16px; }
        .title { font-size: 24px; font-weight: bold; color: #6b2d14; }
        .subtitle { margin-top: 4px; color: #6f5d49; }
        .header-note { font-size: 10px; color: #6f5d49; text-align: right; }
        .section { margin-bottom: 18px; }
        .section-title { font-size: 15px; font-weight: bold; color: #6b2d14; border-left: 5px solid #b5651d; padding-left: 8px; margin-bottom: 10px; }
        .cards { display:flex; gap:10px; margin-bottom: 14px; }
        .card { flex:1; background:#fff; border:1px solid #e2d6c5; border-radius:8px; padding:10px 12px; }
        .card .label { font-size:10px; color:#7c6a57; margin-bottom:6px; }
        .card .value { font-size:21px; font-weight:bold; }
        .c-red { color:#b33a3a; }
        .c-orange { color:#d3701f; }
        .c-blue { color:#1f5f85; }
        .c-green { color:#2f7d4a; }
        .minor { font-size:10px; color:#7c6a57; margin-top:4px; }
        .box { background:#fff; border:1px solid #e2d6c5; border-radius:8px; padding:10px 12px; }
        .bullets { margin:0; padding-left:18px; }
        .bullets li { margin-bottom:6px; }
        .table { width:100%; border-collapse:collapse; background:#fff; }
        .table th, .table td { border:1px solid #e5dbc9; padding:6px 8px; }
        .table th { background:#efe2cf; color:#69482a; }
        .pill { display:inline-block; padding:2px 8px; border-radius:999px; color:#fff; font-size:10px; }
        .pill-4 { background:#b33a3a; }
        .pill-3 { background:#dd7a28; }
        .pill-2 { background:#c79a22; }
        .pill-1 { background:#2b7a90; }
        .grid { display:flex; gap:12px; }
        .col { flex:1; }
        .empty { background:#fff; border:1px dashed #d3c3ac; padding:12px; color:#7c6a57; }
        .footer { margin-top:18px; font-size:10px; color:#6f5d49; text-align:right; }
    </style>
    """

    html = [
        f"<html><head><meta charset='UTF-8'>{css}</head><body><div class='page'>",
        "<div class='header'>",
        "<div>",
        "<div class='title'>周度告警行动简报</div>",
        f"<div class='subtitle'>统计周期：{report_data['start_dt'].strftime('%Y-%m-%d')} 至 {report_data['end_dt'].strftime('%Y-%m-%d')}</div>",
        "</div>",
        "<div class='header-note'>",
        f"受影响系统：<b>{summary['affected_systems']}</b> 个<br>",
        f"日均告警：<b>{avg_daily}</b> 次",
        "</div></div>",
        "<div class='section'>",
        "<div class='section-title'>一、本周概览</div>",
        "<div class='cards'>",
        f"<div class='card'><div class='label'>本周新增告警数</div><div class='value c-red'>{summary['total_alerts']}</div></div>",
        f"<div class='card'><div class='label'>紧急告警 Level 4</div><div class='value c-orange'>{summary['urgent_total']}</div></div>",
        f"<div class='card'><div class='label'>严重及以上 Level 3+</div><div class='value c-blue'>{summary['severe_total']}</div></div>",
        f"<div class='card'><div class='label'>恢复情况</div><div class='value c-green'>{summary['recovery_rate']}</div><div class='minor'>{'平均恢复 ' + str(summary['avg_recovery_hours']) + ' 小时' if summary['avg_recovery_hours'] is not None else '恢复时长暂不可算'}</div></div>",
        "</div>",
        f"<div class='box'>{recovery_note}</div>",
        "</div>",
        "<div class='section'>",
        "<div class='section-title'>二、本周处理重点</div>",
        "<div class='box'><ul class='bullets'>",
    ]

    for item in report_data["action_items"]:
        html.append(f"<li>{item}</li>")

    html.extend(
        [
            "</ul></div>",
            "</div>",
            "<div class='section'>",
            "<div class='section-title'>三、告警趋势</div>",
            f"<div class='box'><img src='data:image/png;base64,{chart_img}' style='width:100%;'></div>",
            "</div>",
            "<div class='section'>",
            "<div class='section-title'>四、重点告警清单</div>",
        ]
    )

    if urgent_focus:
        html.append(
            "<table class='table'><thead><tr><th>系统</th><th>告警名称</th><th>对象</th><th>级别</th><th>次数</th><th>最近发生</th><th>摘要</th></tr></thead><tbody>"
        )
        for row in urgent_focus:
            level_num = row[3] if row[3] in level_map else 1
            html.append(
                "<tr>"
                f"<td>{row[0]}</td>"
                f"<td>{row[2]}</td>"
                f"<td>{row[5]}</td>"
                f"<td><span class='pill pill-{level_num}'>{level_map.get(level_num, '未知')}</span></td>"
                f"<td>{row[8]}</td>"
                f"<td>{str(row[10])[:16]}</td>"
                f"<td>{row[6] or ''}</td>"
                "</tr>"
            )
        html.append("</tbody></table>")
    else:
        html.append("<div class='empty'>本周没有严重及以上告警。</div>")

    html.extend(
        [
            "</div>",
            "<div class='section'>",
            "<div class='section-title'>五、高频系统与高频告警</div>",
            "<div class='grid'>",
            "<div class='col'>",
        ]
    )

    if top_systems:
        html.append("<table class='table'><thead><tr><th>系统</th><th>总数</th><th>紧急</th><th>严重及以上</th></tr></thead><tbody>")
        for system_name, system_data in top_systems:
            html.append(
                "<tr>"
                f"<td>{system_name}</td>"
                f"<td>{system_data['total']}</td>"
                f"<td>{system_data['urgent']}</td>"
                f"<td>{system_data['severe']}</td>"
                "</tr>"
            )
        html.append("</tbody></table>")
    else:
        html.append("<div class='empty'>本周没有系统热度数据。</div>")

    html.extend(["</div>", "<div class='col'>"])

    if top_alerts:
        html.append("<table class='table'><thead><tr><th>告警名称</th><th>系统</th><th>次数</th><th>级别</th></tr></thead><tbody>")
        for row in top_alerts:
            level_num = row[3] if row[3] in level_map else 1
            html.append(
                "<tr>"
                f"<td>{row[2]}</td>"
                f"<td>{row[0]}</td>"
                f"<td>{row[8]}</td>"
                f"<td><span class='pill pill-{level_num}'>{level_map.get(level_num, '未知')}</span></td>"
                "</tr>"
            )
        html.append("</tbody></table>")
    else:
        html.append("<div class='empty'>本周没有高频告警数据。</div>")

    html.extend(["</div>", "</div>", "</div>"])

    html.append("<div class='section'><div class='section-title'>六、遗留告警</div>")
    if summary["recovery_metrics_available"] and stale_rows:
        html.append(
            "<table class='table'><thead><tr><th>系统</th><th>告警名称</th><th>对象</th><th>级别</th><th>开始时间</th><th>持续天数</th><th>摘要</th></tr></thead><tbody>"
        )
        for row in stale_rows:
            level_num = row[3] if row[3] in level_map else 1
            html.append(
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
        html.append("</tbody></table>")
    elif summary["recovery_metrics_available"]:
        html.append(f"<div class='empty'>截至统计结束，没有持续超过 {report_data['stale_days']} 天的未恢复告警。</div>")
    else:
        html.append("<div class='empty'>当前数据库未保存恢复状态，无法自动识别遗留未清零告警。</div>")
    html.append("</div>")

    html.append(
        f"<div class='footer'>生成时间：{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</div>"
    )
    html.append("</div></body></html>")
    return "".join(html)


def write_outputs(html, args):
    html_file = args.html_file or DEFAULT_HTML_FILE
    pdf_file = args.pdf_file or DEFAULT_PDF_FILE

    if args.output_format in {"html", "both"}:
        with open(html_file, "w", encoding="utf-8") as file_obj:
            file_obj.write(html)
        print(f"HTML 已生成: {html_file}")

    if args.output_format in {"pdf", "both"}:
        HTML = get_weasyprint_html()
        try:
            HTML(string=html).write_pdf(pdf_file)
        except OSError as exc:
            raise RuntimeError(
                "PDF 导出失败：当前系统缺少 WeasyPrint 所需的本地图形依赖，建议先生成 HTML。"
            ) from exc
        print(f"PDF 已生成: {pdf_file}")


def main():
    args = parse_args()
    start_dt, end_dt = build_date_range(args)
    report_data = fetch_weekly_report(start_dt, end_dt, args.top_n, args.stale_days)
    if not report_data["summary"]["total_alerts"]:
        print("未查询到数据。")
        return

    html = generate_html(report_data)
    write_outputs(html, args)


if __name__ == "__main__":
    main()
