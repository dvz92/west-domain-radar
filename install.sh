#!/usr/bin/env bash
# ══════════════════════════════════════════════════════════════════
#  过期域名雷达 · 一键安装
#  ------------------------------------------------------------------
#  方式 A（推荐，git clone 后在仓库目录里跑）：
#      git clone https://github.com/<你>/west-domain-radar.git
#      cd west-domain-radar && bash install.sh
#
#  方式 B（不 clone，直接远程跑，需要告诉它仓库地址）：
#      curl -fsSL https://raw.githubusercontent.com/<你>/west-domain-radar/main/install.sh \
#        | REPO_SLUG=<你>/west-domain-radar bash
#
#  可选环境变量：
#      INSTALL_DIR=~/west-radar    安装目录
#      RUN_HOUR_BJ=9               每天几点跑（北京时间）
#      RUN_MIN_BJ=0
#      NO_CRON=1                   不装定时任务
#      SKIP_CORPORA=1              跳过语料下载（已手动放好 assets/ 时用）
#      MAIL_TO / SMTP_HOST / SMTP_PORT / SMTP_SECURITY / SMTP_USER / SMTP_PASS / SMTP_FROM
#  ------------------------------------------------------------------
#  只依赖 python3（≥3.8）+ 标准库，不需要 pip 装任何东西。
# ══════════════════════════════════════════════════════════════════
set -euo pipefail

INSTALL_DIR="${INSTALL_DIR:-$HOME/west-radar}"
RUN_HOUR_BJ="${RUN_HOUR_BJ:-9}"
RUN_MIN_BJ="${RUN_MIN_BJ:-0}"
BRANCH="${BRANCH:-main}"
REPO_SLUG="${REPO_SLUG:-}"

say()  { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[!]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[x]\033[0m %s\n' "$*" >&2; exit 1; }

TMPDIR_X=""
cleanup() { [ -n "$TMPDIR_X" ] && rm -rf "$TMPDIR_X"; }
trap cleanup EXIT

# ───────────────────── 1. 找到仓库文件 ─────────────────────
SRC=""
if [ -n "${BASH_SOURCE[0]:-}" ] && [ "${BASH_SOURCE[0]}" != "bash" ]; then
  HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" 2>/dev/null && pwd || true)"
  if [ -n "${HERE:-}" ] && [ -f "$HERE/scripts/radar.py" ] && [ -f "$HERE/manage.py" ]; then
    SRC="$HERE"
    say "使用本地仓库文件：$SRC"
  fi
fi

if [ -z "$SRC" ]; then
  [ -n "$REPO_SLUG" ] || die "没有检测到本地仓库文件，也没给 REPO_SLUG。
    要么先 git clone 再在仓库目录里执行 bash install.sh，
    要么用：curl -fsSL <install.sh 的 raw 地址> | REPO_SLUG=<你>/west-domain-radar bash"
  command -v curl >/dev/null 2>&1 || command -v wget >/dev/null 2>&1 \
    || die "需要 curl 或 wget 来下载仓库"
  TMPDIR_X="$(mktemp -d)"
  URL="https://codeload.github.com/$REPO_SLUG/tar.gz/refs/heads/$BRANCH"
  say "下载仓库 $REPO_SLUG@$BRANCH"
  if command -v curl >/dev/null 2>&1; then
    curl -fsSL "$URL" | tar xz -C "$TMPDIR_X" || die "下载失败：$URL"
  else
    wget -qO- "$URL" | tar xz -C "$TMPDIR_X" || die "下载失败：$URL"
  fi
  SRC="$(find "$TMPDIR_X" -mindepth 1 -maxdepth 1 -type d | head -1)"
  [ -f "$SRC/scripts/radar.py" ] || die "仓库里没有 scripts/radar.py，检查 REPO_SLUG / BRANCH"
fi

# ───────────────────── 2. python3 ─────────────────────
say "检查 python3"
if ! command -v python3 >/dev/null 2>&1; then
  warn "没装 python3，尝试用系统包管理器安装"
  SUDO=""; [ "$(id -u)" = "0" ] || SUDO="sudo"
  if command -v apt-get >/dev/null 2>&1; then
    $SUDO apt-get update -qq && $SUDO apt-get install -y -qq python3
  elif command -v dnf >/dev/null 2>&1; then
    $SUDO dnf install -y -q python3
  elif command -v yum >/dev/null 2>&1; then
    $SUDO yum install -y -q python3
  elif command -v apk >/dev/null 2>&1; then
    $SUDO apk add --no-cache python3
  fi
fi
command -v python3 >/dev/null 2>&1 || die "python3 仍不可用，请手动安装后重跑"
PY_BIN="$(command -v python3)"
"$PY_BIN" - <<'PY' || die "python3 版本过低（需要 ≥3.8）"
import sys
raise SystemExit(0 if sys.version_info >= (3, 8) else 1)
PY
say "$("$PY_BIN" -V 2>&1)"
export PYTHONIOENCODING=utf-8

# ───────────────────── 3. 复制文件 ─────────────────────
say "安装到 $INSTALL_DIR"
mkdir -p "$INSTALL_DIR" "$INSTALL_DIR/logs" "$INSTALL_DIR/reports" "$INSTALL_DIR/assets"
for item in scripts tools assets manage.py run.sh config.example.env README.md LICENSE; do
  if [ -e "$SRC/$item" ]; then
    cp -R "$SRC/$item" "$INSTALL_DIR/"
  fi
done
rm -rf "$INSTALL_DIR/scripts/__pycache__" "$INSTALL_DIR/tools/__pycache__"
chmod +x "$INSTALL_DIR/run.sh"
[ -f "$INSTALL_DIR/config.env" ] || cp "$INSTALL_DIR/config.example.env" "$INSTALL_DIR/config.env"
say "文件已就位（$(find "$INSTALL_DIR" -type f | wc -l) 个）"

# ───────────────────── 4. 语料 + 索引 ─────────────────────
if [ "${SKIP_CORPORA:-0}" = "1" ]; then
  warn "SKIP_CORPORA=1，跳过语料下载"
else
  say "下载语料（约 22MB，失败会自动换镜像）"
  ( cd "$INSTALL_DIR" && "$PY_BIN" tools/fetch_corpora.py )
fi
say "构建语言索引（幂等，可重复跑）"
( cd "$INSTALL_DIR/scripts" && "$PY_BIN" build_assets.py )
say "自检：语言分类模型"
( cd "$INSTALL_DIR/scripts" && "$PY_BIN" lang.py >/dev/null ) && say "自检通过" \
  || die "自检失败，把上面的报错发我"

# ───────────────────── 5. 邮件配置（可选） ─────────────────────
if [ -n "${MAIL_TO:-}" ]; then
  say "写入邮件配置"
  set_kv() {
    [ -z "${2:-}" ] && return 0
    "$PY_BIN" - "$INSTALL_DIR/config.env" "$1" "$2" <<'PY'
import sys
p, k, v = sys.argv[1], sys.argv[2], sys.argv[3]
lines = open(p, encoding="utf-8").read().split("\n")
for i, l in enumerate(lines):
    if l.startswith(k + "="):
        lines[i] = "%s=%s" % (k, v)
        break
else:
    lines.append("%s=%s" % (k, v))
open(p, "w", encoding="utf-8").write("\n".join(lines))
PY
  }
  set_kv MAIL_TO "${MAIL_TO}"
  set_kv SMTP_HOST "${SMTP_HOST:-}"
  set_kv SMTP_PORT "${SMTP_PORT:-}"
  set_kv SMTP_SECURITY "${SMTP_SECURITY:-}"
  set_kv SMTP_USER "${SMTP_USER:-}"
  set_kv SMTP_PASS "${SMTP_PASS:-}"
  set_kv SMTP_FROM "${SMTP_FROM:-}"
  chmod 600 "$INSTALL_DIR/config.env"
  if [ -n "${SMTP_HOST:-}" ]; then
    say "邮件：$MAIL_TO（经由 $SMTP_HOST）"
  else
    warn "只写了收件人，发件服务器还没配 —— 装完跑 wdradar 选 s 补上"
  fi
fi

# ───────────────────── 6. 短命令 wdradar ─────────────────────
WRAPPER=""
for d in /usr/local/bin "$HOME/.local/bin"; do
  if [ -w "$d" ] 2>/dev/null || { [ ! -e "$d" ] && mkdir -p "$d" 2>/dev/null; }; then
    if [ -w "$d" ]; then
      WRAPPER="$d/wdradar"
      cat >"$WRAPPER" <<EOF
#!/usr/bin/env bash
exec "$PY_BIN" "$INSTALL_DIR/manage.py" "\$@"
EOF
      chmod 755 "$WRAPPER"
      break
    fi
  fi
done
if [ -n "$WRAPPER" ]; then
  say "短命令已安装：$WRAPPER"
else
  warn "没能装短命令，可直接用：python3 $INSTALL_DIR/manage.py"
fi
case ":$PATH:" in
  *":$HOME/.local/bin:"*) ;;
  *) if [ "$WRAPPER" = "$HOME/.local/bin/wdradar" ]; then
       grep -q '.local/bin' "$HOME/.bashrc" 2>/dev/null || \
         echo 'export PATH="$HOME/.local/bin:$PATH"' >>"$HOME/.bashrc"
       warn "已把 ~/.local/bin 加进 ~/.bashrc，重开终端后 wdradar 才生效"
     fi ;;
esac

# ───────────────────── 7. 定时任务 ─────────────────────
if [ "${NO_CRON:-0}" = "1" ]; then
  warn "NO_CRON=1，跳过定时任务"
else
  say "设定定时任务（北京时间 $(printf '%02d:%02d' "$RUN_HOUR_BJ" "$RUN_MIN_BJ")）"
  if "$PY_BIN" "$INSTALL_DIR/manage.py" time "$(printf '%02d:%02d' "$RUN_HOUR_BJ" "$RUN_MIN_BJ")" \
       >/dev/null 2>&1; then
    say "定时任务已装"
  else
    warn "定时任务装失败，装完跑 wdradar 选 t 手动设"
  fi
fi

# ───────────────────── 8. 首跑 ─────────────────────
say "现在就试跑一次（1~3 分钟；同时验证 west.cn 是否放行本机 IP）"
if bash "$INSTALL_DIR/run.sh"; then
  D="$(TZ=Asia/Shanghai date +%F)"
  if [ -f "$INSTALL_DIR/reports/$D.html" ]; then
    say "成功！报告：$INSTALL_DIR/reports/$D.html"
  else
    warn "跑完了但没生成今天的报告，看日志：$INSTALL_DIR/logs/$D.log"
  fi
else
  warn "首跑有异常，看日志：$INSTALL_DIR/logs/$(TZ=Asia/Shanghai date +%F).log"
fi

cat <<EOF

────────────────────────────────────────────────────────────
  安装完成
────────────────────────────────────────────────────────────
  管理面板  ${WRAPPER:-python3 $INSTALL_DIR/manage.py}

            d  立即运行一次      e  设定通知邮箱
            s  设定发件服务器    t  设定每天启动时间
            m  发送测试邮件      r  查看最近报告
            l  查看最近日志      p  暂停/启用定时
            u  完整卸载

  目录      $INSTALL_DIR
  手动跑    bash $INSTALL_DIR/run.sh
  看日志    tail -f $INSTALL_DIR/logs/\$(date +%F).log

  ⚠️ 一天只跑一次。单次约 30 个请求是安全线，当天重复全量跑容易触发
     west.cn 限流（表现为日志里出现「查询太频繁 / code=500」）。
────────────────────────────────────────────────────────────
EOF
