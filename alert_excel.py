import argparse
from datetime import datetime, timedelta

import pandas as pd

from db_utils import get_db_connection, get_db_display_name


def parse_month(month_text=None):
    if not month_text:
        now = datetime.now()
        return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    try:
        parsed = datetime.strptime(month_text, "%Y-%m")
    except ValueError:
        raise ValueError("--month 参数格式必须为 YYYY-MM，例如 2026-06")

    return parsed.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def build_output_filename(month_start, output_path=None):
    if output_path:
        return output_path

    export_time = datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"month_alerts_{month_start.strftime('%Y-%m')}_{export_time}.xlsx"


def export_month_alerts(month_text=None, output_path=None):
    table_name = 'weekly_alerts'
    month_start = parse_month(month_text)
    next_month = (month_start.replace(day=28) + timedelta(days=4)).replace(day=1)
    month_end = next_month - timedelta(seconds=1)
    output_filename = build_output_filename(month_start, output_path)

    try:
        conn = get_db_connection()
        query = f"""
        SELECT
            id, alert_name, cluster, namespace, level,
            metric_type, target, key_info, detail_info,
            fingerprint, first_status, status, starts_at, ends_at,
            created_at, updated_at, resolved_at
        FROM {table_name}
        WHERE created_at BETWEEN %s AND %s
          AND COALESCE(first_status, 'firing') = 'firing'
        ORDER BY created_at DESC
        """

        print(f"🚀 正在从 {get_db_display_name()} 读取 {month_start.strftime('%Y-%m')} 月的告警数据...")
        df = pd.read_sql_query(
            query,
            conn,
            params=[
                month_start.strftime("%Y-%m-%d %H:%M:%S"),
                month_end.strftime("%Y-%m-%d %H:%M:%S"),
            ],
        )

        if df.empty:
            print(f"💡 提示：{month_start.strftime('%Y-%m')} 月没有产生任何告警数据。")
        else:
            df.to_excel(output_filename, index=False, engine='openpyxl')
            print("-" * 30)
            print("✅ 导出成功！")
            print(f"🗓️ 目标月份: {month_start.strftime('%Y-%m')}")
            print(f"📊 记录总数: {len(df)} 条")
            print(f"📁 文件路径: {output_filename}")
            print("-" * 30)

    except Exception as e:
        print(f"❌ 运行出错: {e}")

    finally:
        if 'conn' in locals():
            conn.close()


def main():
    parser = argparse.ArgumentParser(description="导出指定月份的告警数据到 Excel")
    parser.add_argument("--month", help="目标月份，格式 YYYY-MM，例如 2026-06")
    parser.add_argument("--output", help="自定义输出文件名或路径，例如 /tmp/month_alerts_2026-06.xlsx")
    args = parser.parse_args()
    export_month_alerts(month_text=args.month, output_path=args.output)


if __name__ == "__main__":
    main()
