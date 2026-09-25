#!/usr/bin/env bash
# 过期域名雷达 · 每日运行入口（由 cron 调用；也可手动跑）
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# 站点按中国时间发布删除日期，必须用北京时间算"今天"
export TZ="${TZ_OVERRIDE:-Asia/Shanghai}"

[ -f "$HERE/config.env" ] && . "$HERE/config.env"
PY="${PYTHON:-python3}"

mkdir -p "$HERE/logs" "$HERE/reports"
DATE="$(date +%F)"
LOG="$HERE/logs/$DATE.log"

{
  echo "════════════════════════════════════════════"
  echo "过期域名雷达　$(date '+%F %T %Z')"
  echo "════════════════════════════════════════════"

  cd "$HERE/scripts" || exit 1
  "$PY" -u radar.py
  code=$?
  echo "── radar.py 退出码 = $code"

  if [ "$code" -eq 0 ]; then
    "$PY" -u "$HERE/tools/notify.py" --date "$DATE"
    echo "── 推送退出码 = $?"
  else
    echo "── 未取到数据：被 west.cn 限流，或抓取结果为空。"
    echo "── 按设计不发空报告、不编造域名。"
    "$PY" -u "$HERE/tools/notify.py" --date "$DATE" --failure \
      --reason "radar.py 退出码 $code（west.cn 限流或抓取结果为空）"
    echo "── 失败告知退出码 = $?"
  fi
  echo
} >>"$LOG" 2>&1

# 只保留最近 60 天，避免磁盘被慢慢吃满
find "$HERE/reports" -maxdepth 1 -type f -mtime +60 -delete 2>/dev/null
find "$HERE/logs" -maxdepth 1 -type f -mtime +60 -delete 2>/dev/null
find "$HERE/scripts" "$HERE/tools" -maxdepth 1 -type d -name '__pycache__' -exec rm -rf {} + 2>/dev/null

exit 0
