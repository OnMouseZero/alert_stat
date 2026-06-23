import os
import sys
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))


from alert_dashboard import app, logger
from db_utils import get_db_display_name


PORT = int(os.getenv("ALERT_DASHBOARD_HTTP_PORT", "5003"))


if __name__ == "__main__":
    logger.info("告警看板 HTTP 服务启动，端口: %s, db=%s", PORT, get_db_display_name())
    app.run(host="0.0.0.0", port=PORT)
