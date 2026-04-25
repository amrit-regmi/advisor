#!/bin/bash
source /home/ubuntu/venv/bin/activate
cd /home/ubuntu/advisor
echo "=== $(date) Mid-day check ==="
python portfolio/midday_check.py
echo "=== $(date) Mid-day check complete ==="
