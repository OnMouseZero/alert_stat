import sqlite3
import datetime
import logging
import os
import base64
from io import BytesIO
from collections import defaultdict
import matplotlib.pyplot as plt
from weasyprint import HTML

# ================= 配置区 =================
DB_FILE = 'alerts.db'
REPORT_FILENAME = 'weekly_inspection_report_custom.pdf'
TOP_N_ALERTS = 3
TOTAL_INTEGRATED_SYSTEMS = 12  # 产投目前接入的系统总数

# 中文字体配置 (确保绘图不乱码)
plt.rcParams['font.sans-serif'] = ['SimHei', 'WenQuanYi Micro Hei', 'DejaVu Sans'] 
plt.rcParams['axes.unicode_minus'] = False
# =========================================

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s')

def get_weekly_alerts(start_date_str=None, end_date_str=None):
    """查询指定日期范围的数据及趋势"""
    if not os.path.exists(DB_FILE):
        logging.warning(f"数据库文件 {DB_FILE} 不存在")
        return [], {}, (None, None), {}

    # 日期解析逻辑
    if start_date_str and end_date_str:
        start_dt = datetime.datetime.strptime(start_date_str, '%Y.%m.%d')
        end_dt = datetime.datetime.strptime(end_date_str, '%Y.%m.%d').replace(hour=23, minute=59, second=59)
    else:
        end_dt = datetime.datetime.now()
        start_dt = end_dt - datetime.timedelta(days=7)

    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    start_fmt = start_dt.strftime('%Y-%m-%d %H:%M:%S')
    end_fmt = end_dt.strftime('%Y-%m-%d %H:%M:%S')

    try:
        # 1. 查询明细
        sql = """
        SELECT t1.cluster, t1.namespace, t1.alert_name, MAX(t1.level) as max_level,
               MAX(t1.metric_type), t1.target,
               (SELECT key_info FROM weekly_alerts t2 WHERE t2.cluster = t1.cluster AND t2.namespace = t1.namespace AND t2.alert_name = t1.alert_name AND t2.target = t1.target ORDER BY t2.starts_at DESC LIMIT 1),
               (SELECT detail_info FROM weekly_alerts t3 WHERE t3.cluster = t1.cluster AND t3.namespace = t1.namespace AND t3.alert_name = t1.alert_name AND t3.target = t1.target ORDER BY t3.starts_at DESC LIMIT 1),
               COUNT(*) as frequency, MIN(t1.starts_at), MAX(t1.starts_at)
        FROM weekly_alerts t1
        WHERE t1.created_at BETWEEN ? AND ?
          AND COALESCE(t1.first_status, 'firing') = 'firing'
        GROUP BY t1.cluster, t1.namespace, t1.alert_name, t1.target
        ORDER BY t1.cluster ASC, max_level DESC, frequency DESC
        """
        c.execute(sql, (start_fmt, end_fmt))
        rows = c.fetchall()

        # 2. 查询每日趋势
        trend_sql = """
        SELECT strftime('%m-%d', created_at) as day, COUNT(*)
        FROM weekly_alerts
        WHERE created_at BETWEEN ? AND ?
          AND COALESCE(first_status, 'firing') = 'firing'
        GROUP BY day
        """
        c.execute(trend_sql, (start_fmt, end_fmt))
        trend_data = dict(c.fetchall())

        # 3. 查询恢复统计
        summary_sql = """
        SELECT
            SUM(CASE
                    WHEN created_at BETWEEN ? AND ?
                     AND COALESCE(first_status, 'firing') = 'firing'
                    THEN 1 ELSE 0
                END) as triggered_total,
            SUM(CASE
                    WHEN created_at BETWEEN ? AND ?
                     AND COALESCE(first_status, 'firing') = 'firing'
                     AND COALESCE(status, 'firing') = 'resolved'
                    THEN 1 ELSE 0
                END) as resolved_current_total,
            SUM(CASE
                    WHEN created_at BETWEEN ? AND ?
                     AND COALESCE(first_status, 'firing') = 'firing'
                     AND COALESCE(status, 'firing') != 'resolved'
                    THEN 1 ELSE 0
                END) as unresolved_total,
            SUM(CASE
                    WHEN resolved_at BETWEEN ? AND ?
                     AND COALESCE(first_status, 'firing') = 'firing'
                    THEN 1 ELSE 0
                END) as recovered_in_period,
            AVG(CASE
                    WHEN resolved_at BETWEEN ? AND ?
                     AND COALESCE(first_status, 'firing') = 'firing'
                     AND starts_at IS NOT NULL AND starts_at != ''
                    THEN (julianday(resolved_at) - julianday(starts_at)) * 24
                    ELSE NULL
                END) as avg_recovery_hours
        FROM weekly_alerts
        """
        c.execute(
            summary_sql,
            (
                start_fmt, end_fmt,
                start_fmt, end_fmt,
                start_fmt, end_fmt,
                start_fmt, end_fmt,
                start_fmt, end_fmt,
            )
        )
        summary_row = c.fetchone() or (0, 0, 0, 0, None)
        summary = {
            'triggered_total': summary_row[0] or 0,
            'resolved_current_total': summary_row[1] or 0,
            'unresolved_total': summary_row[2] or 0,
            'recovered_in_period': summary_row[3] or 0,
            'avg_recovery_hours': round(summary_row[4], 2) if summary_row[4] is not None else None,
        }

        return rows, trend_data, (start_dt, end_dt), summary
    finally:
        conn.close()

def generate_trend_chart(trend_data, start_dt, end_dt):
    """绘制趋势图"""
    days, counts = [], []
    curr = start_dt
    while curr <= end_dt:
        d_str = curr.strftime('%m-%d')
        days.append(d_str)
        counts.append(trend_data.get(d_str, 0))
        curr += datetime.timedelta(days=1)

    plt.figure(figsize=(10, 3.5))
    bars = plt.bar(days, counts, color='#3498db', alpha=0.7, width=0.5)
    plt.plot(days, counts, marker='o', color='#e74c3c', linewidth=2, markersize=4)
    plt.title('告警数量每日趋势走势', fontsize=12, pad=10)
    plt.grid(axis='y', linestyle='--', alpha=0.4)
    
    # 在柱状图上方标注数值
    for bar in bars:
        height = bar.get_height()
        plt.text(bar.get_x() + bar.get_width()/2., height + 0.1, f'{int(height)}', ha='center', va='bottom', fontsize=9)

    buf = BytesIO()
    plt.savefig(buf, format='png', bbox_inches='tight', dpi=100)
    plt.close()
    return base64.b64encode(buf.getvalue()).decode('utf-8')

def generate_html(alerts, trend_data, date_range, summary):
    start_dt, end_dt = date_range
    systems_data = defaultdict(lambda: {'total': 0, 'levels': {4:0,3:0,2:0,1:0}, 'rows': []})
    global_levels = {4:0,3:0,2:0,1:0}
    global_alert_names = defaultdict(int)

    for row in alerts:
        cluster, alert_name, freq = row[0], row[2], row[8]
        try: level = int(row[3])
        except: level = 1
        systems_data[cluster]['rows'].append(row)
        systems_data[cluster]['total'] += freq
        systems_data[cluster]['levels'][level] += freq 
        global_levels[level] += freq
        global_alert_names[alert_name] += freq

    # 提取全局告警Top和系统告警Top
    top_alerts_data = sorted(global_alert_names.items(), key=lambda x: x[1], reverse=True)[:TOP_N_ALERTS]
    top_systems_data = sorted(systems_data.items(), key=lambda x: x[1]['total'], reverse=True)[:TOP_N_ALERTS]
    
    chart_img = generate_trend_chart(trend_data, start_dt, end_dt)
    recovery_rate = 0
    if summary['triggered_total']:
        recovery_rate = round(summary['resolved_current_total'] / summary['triggered_total'] * 100, 1)

    # 告警级别映射字典
    LEVEL_MAP = {4: '紧急', 3: '严重', 2: '中度', 1: '轻微'}

    # CSS 样式
    css = """
    <style>
        @page { margin: 1cm; size: A4; }
        body { font-family: "WenQuanYi Micro Hei", sans-serif; font-size: 11px; color: #333; line-height: 1.4; position: relative; }
        .report-header { text-align: center; border-bottom: 2px solid #2c3e50; padding-bottom: 10px; margin-bottom: 15px; position: relative; }
        .header-stats { position: absolute; top: 0; right: 0; text-align: right; background: #f8f9fa; padding: 5px 10px; border: 1px solid #ddd; border-radius: 4px; font-size: 10px; }
        .global-summary-box { background: #f8f9fa; border: 1px solid #ddd; border-radius: 6px; padding: 12px; margin-bottom: 20px; }
        .summary-top-row { display: flex; justify-content: space-between; margin-bottom: 10px; font-weight: bold; }
        .level-card { background: #fff; padding: 5px; border-radius: 4px; border: 1px solid #e0e0e0; text-align: center; min-width: 70px; }
        .lifecycle-summary { display:flex; justify-content:space-between; gap:10px; margin-bottom:20px; }
        .lifecycle-card { flex:1; background:#fff; border:1px solid #ddd; border-radius:6px; padding:10px 12px; }
        .lifecycle-card .title { color:#666; font-size:10px; margin-bottom:4px; }
        .lifecycle-card .value { font-size:18px; font-weight:bold; }
        
        /* 左右分栏布局，用于展示两个 Top 3 */
        .top-cards-wrapper { display: flex; justify-content: space-between; margin-bottom: 20px; }
        .top-alerts-container { width: 48%; border: 1px solid #ebccd1; border-radius: 4px; overflow: hidden; }
        .top-header { background: #f2dede; color: #a94442; padding: 6px 12px; font-weight: bold; }
        .top-systems-container { width: 48%; border: 1px solid #bce8f1; border-radius: 4px; overflow: hidden; }
        .top-systems-header { background: #d9edf7; color: #31708f; padding: 6px 12px; font-weight: bold; }
        
        .top-table { width: 100%; border-collapse: collapse; }
        .top-table td { padding: 6px 12px; border-bottom: 1px solid #eee; }
        .rank-badge { display: inline-block; width: 18px; height: 18px; line-height: 18px; text-align: center; border-radius: 50%; color: #fff; font-size: 10px; background: #999; margin-right: 5px; }
        .rank-1 { background: #d9534f; } .rank-2 { background: #fd7e14; } .rank-3 { background: #ffc107; color:#333; }
        .progress-bar-bg { background: #eee; height: 5px; width: 60px; border-radius: 3px; display: inline-block; vertical-align: middle; }
        .progress-bar-fill { background: #d9534f; height: 100%; border-radius: 3px; }
        .progress-bar-fill-sys { background: #31708f; height: 100%; border-radius: 3px; }
        
        .trend-container { text-align: center; margin-bottom: 25px; border: 1px solid #ddd; padding: 10px; border-radius: 6px; }
        .system-section { margin-bottom: 25px; page-break-inside: avoid; }
        .system-title { font-size: 14px; font-weight: bold; border-left: 4px solid #007bff; padding-left: 8px; margin-bottom: 8px; }
        .system-stats { font-size: 11px; background: #fff; padding: 6px 10px; border: 1px dashed #ccc; margin-bottom: 8px; color: #555; }
        .stat-badge { padding: 1px 5px; border-radius: 3px; color: #fff; font-size: 10px; margin: 0 2px; }
        .bg-4 { background-color: #d9534f; } .bg-3 { background-color: #fd7e14; } .bg-2 { background-color: #ffc107; color: #333; } .bg-1 { background-color: #17a2b8; }
        table.detail-table { width: 100%; border-collapse: collapse; table-layout: fixed; }
        table.detail-table th, table.detail-table td { border: 1px solid #dee2e6; padding: 5px; word-wrap: break-word; }
        table.detail-table th { background: #f8f9fa; }
    </style>
    """

    html = f"""<html><head><meta charset="UTF-8">{css}</head><body>
        <div class="report-header">
            <h1>运维巡检周报</h1>
            <div class="meta">统计周期: {start_dt.strftime('%Y.%m.%d')} - {end_dt.strftime('%Y.%m.%d')}</div>
            <div class="header-stats">
                产投接入总系统: <b>{TOTAL_INTEGRATED_SYSTEMS}</b> 个<br>
                本周产生告警系统: <b style="color:#d9534f;">{len(systems_data)}</b> 个
            </div>
        </div>
        
        <div class="global-summary-box">
            <div class="summary-top-row"><span>🚨 本周期触发告警总数: <span style="color:#d9534f; font-size:14px;">{summary['triggered_total']}</span> 次</span></div>
            <div style="display:flex; justify-content: space-around;">
                <div class="level-card" style="border-bottom:3px solid #d9534f;">紧急: {global_levels[4]}</div>
                <div class="level-card" style="border-bottom:3px solid #fd7e14;">严重: {global_levels[3]}</div>
                <div class="level-card" style="border-bottom:3px solid #ffc107;">中度: {global_levels[2]}</div>
                <div class="level-card" style="border-bottom:3px solid #17a2b8;">轻微: {global_levels[1]}</div>
            </div>
        </div>

        <div class="lifecycle-summary">
            <div class="lifecycle-card">
                <div class="title">截至生成时已恢复</div>
                <div class="value" style="color:#28a745;">{summary['resolved_current_total']}</div>
            </div>
            <div class="lifecycle-card">
                <div class="title">截至生成时未恢复</div>
                <div class="value" style="color:#d9534f;">{summary['unresolved_total']}</div>
            </div>
            <div class="lifecycle-card">
                <div class="title">本周期内恢复次数</div>
                <div class="value" style="color:#17a2b8;">{summary['recovered_in_period']}</div>
            </div>
            <div class="lifecycle-card">
                <div class="title">恢复率 / 平均恢复时长</div>
                <div class="value" style="color:#333;">{recovery_rate}%</div>
                <div style="font-size:11px; color:#666; margin-top:4px;">{summary['avg_recovery_hours'] if summary['avg_recovery_hours'] is not None else '-'} 小时</div>
            </div>
        </div>

        <div class="top-cards-wrapper">
            <div class="top-alerts-container">
                <div class="top-header">🏆 全局高频告警 Top {TOP_N_ALERTS}</div>
                <table class="top-table">"""
    
    max_f_alert = top_alerts_data[0][1] if top_alerts_data else 1
    for i, (name, count) in enumerate(top_alerts_data):
        html += f"""<tr><td style="width:30px;"><span class="rank-badge rank-{i+1}">{i+1}</span></td>
                    <td><b>{name}</b></td>
                    <td style="text-align:right; width:100px;"><span style="color:#d9534f;">{count} 次</span>
                    <div class="progress-bar-bg"><div class="progress-bar-fill" style="width:{int(count/max_f_alert*100)}%;"></div></div></td></tr>"""
    html += """</table></div>
            
            <div class="top-systems-container">
                <div class="top-systems-header">🏢 系统告警总数 Top {TOP_N_ALERTS}</div>
                <table class="top-table">"""
                
    max_f_sys = top_systems_data[0][1]['total'] if top_systems_data else 1
    for i, (sys_name, data) in enumerate(top_systems_data):
        sys_count = data['total']
        html += f"""<tr><td style="width:30px;"><span class="rank-badge rank-{i+1}">{i+1}</span></td>
                    <td><b>{sys_name}</b></td>
                    <td style="text-align:right; width:100px;"><span style="color:#31708f;">{sys_count} 次</span>
                    <div class="progress-bar-bg"><div class="progress-bar-fill-sys" style="width:{int(sys_count/max_f_sys*100)}%;"></div></div></td></tr>"""
    html += "</table></div></div>"

    html += f'<div class="trend-container"><div style="text-align:left; font-weight:bold; margin-bottom:5px;">📊 告警趋势图</div><img src="data:image/png;base64,{chart_img}" style="width:100%;"></div>'

    # 系统明细
    for cluster, data in sorted(systems_data.items(), key=lambda x: x[1]['total'], reverse=True):
        l = data['levels']
        html += f"""
        <div class="system-section">
            <div class="system-title">系统名称：{cluster}</div>
            <div class="system-stats">
                <strong>【本周统计】</strong> 告警总数: <b>{data['total']}</b> 次 
                &nbsp;&nbsp;分布: 
                <span class="stat-badge bg-4">紧急</span> {l[4]} 
                <span class="stat-badge bg-3">严重</span> {l[3]} 
                <span class="stat-badge bg-2">中度</span> {l[2]} 
                <span class="stat-badge bg-1">轻微</span> {l[1]}
            </div>
            <table class="detail-table">
                <thead><tr><th style="width:20%;">告警名称</th><th style="width:18%;">对象</th><th style="width:10%;">级别</th><th style="width:8%;">频次</th><th style="width:15%;">最近发生</th><th>摘要</th></tr></thead>
                <tbody>"""
        for r in data['rows']:
            # 转换数字为中文
            level_num = int(r[3]) if str(r[3]).isdigit() else 1
            level_text = LEVEL_MAP.get(level_num, '未知')
            
            html += f"""<tr><td>{r[2]}</td><td>{r[5]}</td>
                        <td style="text-align:center;"><span class="stat-badge bg-{level_num}">{level_text}</span></td>
                        <td style="text-align:center;">{r[8]}</td>
                        <td>{str(r[10])[:16]}</td><td>{r[6] if r[6] else ''}</td></tr>"""
        html += "</tbody></table></div>"

    html += "</body></html>"
    return html

if __name__ == "__main__":
    print("请输入自定义日期范围（例如 2026.2.9），直接回车则统计过去7天")
    s_in = input("开始日期: ").strip() or None
    e_in = input("结束日期: ").strip() or None
    
    rows, trend, dr, summary = get_weekly_alerts(s_in, e_in)
    if summary.get('triggered_total', 0) == 0:
        print("未查询到数据。")
    else:
        html = generate_html(rows, trend, dr, summary)
        HTML(string=html).write_pdf(REPORT_FILENAME)
        print(f"成功生成: {REPORT_FILENAME}")
