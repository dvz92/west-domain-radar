#!/usr/bin/env bash
# 过期域名雷达 · 每日运行入口（由 cron 调用；也可手动跑）
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ⚠️ 顺序很重要：必须先读 config.env，再据此设 TZ。
# （原来写成先 export TZ 再 source，结果 config.env 里的 TZ_OVERRIDE 永远不生效。）
[ -f "$HERE/config.env" ] && . "$HERE/config.env"

# 站点按中国时间发布删除日期，必须用北京时间算"今天"
export TZ="${TZ_OVERRIDE:-Asia/Shanghai}"

mkdir -p "$HERE/logs" "$HERE/reports"
DATE="$(date +%F)"
LOG="$HERE/logs/$DATE.log"

# 怎么找 python3：
#   1) config.env 里 PYTHON= 的绝对路径（安装脚本会写好）
#   2) PATH 里的 python3 / python
# cron 的 PATH 很干净，所以第 1 条往往才是救命的那个。
resolve_python() {
  if [ -n "${PYTHON:-}" ] && [ -x "${PYTHON}" ]; then
    printf '%s' "$PYTHON"; return 0
  fi
  local c
  for c in python3 python; do
    if command -v "$c" >/dev/null 2>&1; then
      command -v "$c"; return 0
    fi
  done
  return 1
}

{
  echo "════════════════════════════════════════════"
  echo "过期域名雷达　$(date '+%F %T %Z')"
  echo "════════════════════════════════════════════"

  if ! PY="$(resolve_python)"; then
    echo "✗ 找不到 python3。请在 $HERE/config.env 里设 PYTHON=/绝对/路径/python3"
    exit 1
  fi
  echo "解释器：$PY　时区：$TZ　日期：$DATE"

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
