import sqlite3
import pandas as pd
from datetime import datetime, timedelta

def export_last_day_alerts():
    # --- 配置信息 ---
    db_file = 'alerts.db'
    table_name = 'weekly_alerts'
    # 生成导出的文件名，如: weekly_alerts_20260227.xlsx
    output_filename = f'month_alerts_{datetime.now().strftime("%Y%m%d")}.xlsx'
    month_start = datetime.now().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    next_month = (month_start.replace(day=28) + timedelta(days=4)).replace(day=1)
    month_end = next_month - timedelta(seconds=1)

    try:
        # 1. 连接数据库
        conn = sqlite3.connect(db_file)
        
        # 2. 构建查询语句
        # 使用 sqlite 的 datetime 函数筛选过去 24 小时的数据
        # 'localtime' 参数确保与系统本地时间一致
        query = f"""
        SELECT 
            id, alert_name, cluster, namespace, level, 
            metric_type, target, key_info, detail_info, 
            fingerprint, first_status, status, starts_at, ends_at,
            created_at, updated_at, resolved_at
        FROM {table_name}
        WHERE created_at BETWEEN '{month_start.strftime("%Y-%m-%d %H:%M:%S")}' AND '{month_end.strftime("%Y-%m-%d %H:%M:%S")}'
          AND COALESCE(first_status, 'firing') = 'firing'
        ORDER BY created_at DESC
        """
        
        print(f"🚀 正在从 {db_file} 读取 {month_start.strftime('%Y-%m')} 月的告警数据...")
        
        # 3. 执行查询并加载到 Pandas DataFrame
        df = pd.read_sql_query(query, conn)
        
        # 4. 检查是否有数据
        if df.empty:
            print("💡 提示：最近 1 个月内没有产生任何告警数据。")
        else:
            # 5. 导出到 Excel
            # index=False 表示不保存每行的序号索引
            df.to_excel(output_filename, index=False, engine='openpyxl')
            
            print("-" * 30)
            print(f"✅ 导出成功！")
            print(f"📊 记录总数: {len(df)} 条")
            print(f"📁 文件路径: {output_filename}")
            print("-" * 30)

    except sqlite3.Error as e:
        print(f"❌ 数据库错误: {e}")
    except Exception as e:
        print(f"❌ 运行出错: {e}")
        
    finally:
        # 6. 确保关闭连接
        if 'conn' in locals():
            conn.close()

if __name__ == "__main__":
    export_last_day_alerts()
