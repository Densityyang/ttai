#!/bin/bash
pkill -f "main.py prod" 2>/dev/null || true
sleep 2
export PATH=/root/.local/bin:$PATH
cd /opt/tt-ai-main || exit 1
: > /tmp/tt-ai.log
nohup uv run python main.py prod >> /tmp/tt-ai.log 2>&1 &
sleep 18
echo "=== log tail ==="
tail -60 /tmp/tt-ai.log
echo "=== curl docs ==="
# AUTH_ENABLED=false 时 /docs 关闭，仅探测端口
curl -s -o /dev/null -w "port8000 %{http_code}\n" http://127.0.0.1:8000/ || echo fail
