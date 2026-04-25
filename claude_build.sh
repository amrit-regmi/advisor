#!/bin/bash
set -e
cd /home/ubuntu/advisor
source /home/ubuntu/venv/bin/activate
export HOME=/home/ubuntu
export PATH=/home/ubuntu/.npm-global/bin:$PATH

echo "=== Build started $(date) ===" >> logs/claude_build.log

claude "Read CLAUDE.md carefully. Read ALL existing code before writing anything. Build every phase in order. Test each phase before moving to next. You have full freedom to refactor anything. Announce each phase completion clearly." 2>&1 | tee -a logs/claude_build.log

echo "=== Build ended $(date) ===" >> logs/claude_build.log
