#!/bin/bash
set -e

# Initialize / migrate the database
python -c "from tools.diff_jobs import init_db, migrate_db; init_db(); migrate_db()"

# Background scraper loop: runs every TASK_INTERVAL_MINUTES (default 5)
INTERVAL=${TASK_INTERVAL_MINUTES:-5}
(
  while true; do
    python run.py 2>&1 | ts '[%Y-%m-%d %H:%M:%S]' || true
    sleep $(( INTERVAL * 60 ))
  done
) &

# Foreground: Streamlit dashboard
exec streamlit run app.py \
  --server.port=8501 \
  --server.address=0.0.0.0 \
  --server.headless=true \
  --browser.gatherUsageStats=false
