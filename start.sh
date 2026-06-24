#!/bin/bash
PIDFILE="/home/sawn1029/weekly-report/server.pid"
LOGFILE="/home/sawn1029/weekly-report/server.log"

# 이미 실행 중이면 종료
if [ -f "$PIDFILE" ] && kill -0 $(cat "$PIDFILE") 2>/dev/null; then
    echo "이미 실행 중 (PID: $(cat $PIDFILE))"
    exit 0
fi

cd /home/sawn1029/weekly-report
nohup python3 server.py > "$LOGFILE" 2>&1 &
echo $! > "$PIDFILE"
echo "주간보고 서버 시작 (PID: $!)"
echo "접속: http://localhost:8081"
