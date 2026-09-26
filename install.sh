#!/usr/bin/env bash
# ══════════════════════════════════════════════════════════════════
#  过期域名雷达 · 一键安装
#  ------------------------------------------------------------------
#  方式 A（推荐，git clone 后在仓库目录里跑）：
#      git clone https://github.com/dvz92/west-domain-radar.git
#      cd west-domain-radar && bash install.sh
#      （克隆目录可以随便放；直接克隆到 ~/west-radar 就地安装也行，脚本会自动跳过复制）
#
#  方式 B（不 clone，直接远程跑，需要告诉它仓库地址）：
#      curl -fsSL https://raw.githubusercontent.com/dvz92/west-domain-radar/main/install.sh \
#        | REPO_SLUG=dvz92/west-domain-radar bash
#
#  可选环境变量：
#      INSTALL_DIR=~/west-radar    安装目录
#      RUN_HOUR_BJ=9               每天几点跑（北京时间）
#      RUN_MIN_BJ=0
#      NO_CRON=1                   不装定时任务
#      SKIP_RUN=1                  装完不试跑（当天已经跑过、不想再耗配额时用）
#      SKIP_CORPORA=1              跳过语料下载（已手动放好 assets/ 时用）
#      REPO_SLUG=user/repo         不 clone 时的仓库地址
#      NOTIFY_CHANNELS=wecom,email 推送渠道顺序（留空=按填了哪些参数自动推断）
#      WECOM_WEBHOOK= / DINGTALK_WEBHOOK= / DINGTALK_SECRET= / FEISHU_WEBHOOK=
#      SERVERCHAN_KEY= / PUSHPLUS_TOKEN= / TG_BOT_TOKEN= / TG_CHAT_ID=
#      NTFY_TOPIC= / NTFY_SERVER= / CUSTOM_WEBHOOK=
#      MAIL_TO / SMTP_HOST / SMTP_PORT / SMTP_SECURITY / SMTP_USER / SMTP_PASS / SMTP_FROM
#      —— 上面这些推送变量只有传了非空值才会写进 config.env，没传的一律不动
#  ------------------------------------------------------------------
#  只依赖 python3（≥3.8）+ 标准库，不需要 pip 装任何东西。
#  脚本幂等：重复运行只覆盖程序文件，不动 config.env / reports / logs，
#            也不会把定时任务加成两条。
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

# 幂等地改一行配置（键不存在就追加）。值里可能有 = / : / 特殊字符，所以用 argv 传参。
# 依赖 $PY_BIN（第 2 步之后才有），所以只能在第 2 步之后调用。
set_kv() {
  [ -z "${2:-}" ] && return 0
  "$PY_BIN" - "$INSTALL_DIR/config.env" "$1" "$2" <<'PY'
import sys
p, k, v = sys.argv[1], sys.argv[2], sys.argv[3]
with open(p, encoding="utf-8") as f:
    lines = f.read().split("\n")
for i, l in enumerate(lines):
    if l.startswith(k + "="):
        lines[i] = "%s=%s" % (k, v)
        break
else:
    lines.append("%s=%s" % (k, v))
with open(p, "w", encoding="utf-8") as f:
    f.write("\n".join(lines))
PY
}

TMPDIR_X=""
cleanup() { [ -n "$TMPDIR_X" ] && rm -rf "$TMPDIR_X" 2>/dev/null; return 0; }
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

# ───────────────────── 2.5 时区数据（决定"今天"是哪天，错了就全错）─────────────────────
# run.sh 里写死 TZ=Asia/Shanghai 来算"今天"和删除日期。精简版 Debian/Alpine 容器
# 常常没装 tzdata，此时 TZ 会被静默忽略、date 返回 UTC → 报表日期和删除日期整体偏一天。
BJ_OFF="$(TZ=Asia/Shanghai date +%z 2>/dev/null || true)"
if [ "$BJ_OFF" = "+0800" ]; then
  say "时区数据 OK（TZ=Asia/Shanghai → +0800）"
else
  warn "缺少 tzdata：TZ=Asia/Shanghai 解析成了 '${BJ_OFF:-空}'，会算错\"今天\" —— 尝试安装"
  SUDO=""; [ "$(id -u)" = "0" ] || SUDO="sudo"
  if   command -v apt-get >/dev/null 2>&1; then $SUDO apt-get install -y -qq tzdata
  elif command -v dnf     >/dev/null 2>&1; then $SUDO dnf install -y -q tzdata
  elif command -v yum     >/dev/null 2>&1; then $SUDO yum install -y -q tzdata
  elif command -v apk     >/dev/null 2>&1; then $SUDO apk add --no-cache tzdata
  else warn "没有可用的包管理器，装不了 tzdata"
  fi
  BJ_OFF="$(TZ=Asia/Shanghai date +%z 2>/dev/null || true)"
  if [ "$BJ_OFF" != "+0800" ]; then
    # 兜底：用 POSIX 固定偏移写法（中国不实行夏令时，UTC-8 等价于 UTC+8，永远不会算错）
    # 注意这里只记下决定，真正的写入放到第 5 步（那时 config.env 才存在，不能提前造一个空文件）
    warn "仍不可用 → 改用固定偏移 TZ=UTC-8（中国无夏令时，效果等价）"
    TZ_OVERRIDE_FALLBACK="UTC-8"
    BJ_OFF="$(TZ=UTC-8 date +%z 2>/dev/null || true)"
    [ "$BJ_OFF" = "+0800" ] || die "时区仍然不对（得到 '${BJ_OFF:-空}'），请手动安装 tzdata 后重跑"
    say "固定偏移可用（待写入 TZ_OVERRIDE=UTC-8）"
  else
    say "tzdata 已装上，时区 OK"
  fi
fi

# ───────────────────── 3. 复制文件 ─────────────────────
say "安装到 $INSTALL_DIR"
mkdir -p "$INSTALL_DIR" "$INSTALL_DIR/logs" "$INSTALL_DIR/reports" "$INSTALL_DIR/assets"
SRC_REAL="$(cd "$SRC" && pwd -P)"
DST_REAL="$(cd "$INSTALL_DIR" && pwd -P)"
if [ "$SRC_REAL" = "$DST_REAL" ]; then
  # 就地安装（例如 git clone 到 ~/west-radar 后直接在里跑）。
  # 必须跳过复制：cp -R 自己到自己会报 "are the same file"，配合 set -e 会直接中断。
  say "源码目录与安装目录相同，跳过复制（就地安装）"
else
  for item in scripts tools assets manage.py run.sh config.example.env README.md LICENSE; do
    if [ -e "$SRC/$item" ]; then
      cp -R "$SRC/$item" "$INSTALL_DIR/"
    fi
  done
fi
# 清理源码带来的 __pycache__（纯清理，失败不能影响安装 —— 所以必须 || true）
rm -rf "$INSTALL_DIR/scripts/__pycache__" "$INSTALL_DIR/tools/__pycache__" 2>/dev/null || true
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

# ───────────────────── 5. 运行配置（可选，从环境变量来） ─────────────────────
# 这些变量只要传了非空值就会被写进 config.env；没传的一律不动（保留已有配置）。
CFG_KEYS="NOTIFY_CHANNELS NOTIFY_ON_FAILURE WECOM_WEBHOOK DINGTALK_WEBHOOK DINGTALK_SECRET
          FEISHU_WEBHOOK SERVERCHAN_KEY PUSHPLUS_TOKEN TG_BOT_TOKEN TG_CHAT_ID
          NTFY_TOPIC NTFY_SERVER CUSTOM_WEBHOOK
          MAIL_TO SMTP_HOST SMTP_PORT SMTP_SECURITY SMTP_USER SMTP_PASS SMTP_FROM"

# python3 的绝对路径：cron 的 PATH 很干净（通常只有 /usr/bin:/bin），
# 裸写 `python3` 在 python 装在别处时会找不到 → 定时任务静默失败。所以钉死绝对路径。
set_kv PYTHON "$PY_BIN"
say "已记录解释器：PYTHON=$PY_BIN"

# 时区兜底（第 2.5 步检测出来的）
[ -n "${TZ_OVERRIDE_FALLBACK:-}" ] && set_kv TZ_OVERRIDE "$TZ_OVERRIDE_FALLBACK"

WROTE=0
for k in $CFG_KEYS; do
  v="${!k:-}"                      # bash 间接取值；未设置或为空 → 跳过
  [ -n "$v" ] || continue
  set_kv "$k" "$v"
  WROTE=$((WROTE + 1))
done

chmod 600 "$INSTALL_DIR/config.env"
if [ "$WROTE" -gt 0 ]; then
  say "已写入 $WROTE 项推送配置"
  "$PY_BIN" "$INSTALL_DIR/manage.py" channels 2>/dev/null || true
else
  say "没传推送配置 —— 装完跑 wdradar 选 n 再配（推荐企业微信机器人，最稳）"
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
if [ "${SKIP_RUN:-0}" = "1" ]; then
  warn "SKIP_RUN=1，跳过首跑（稍后自己跑：wdradar run）"
else
  say "现在就试跑一次（1~3 分钟；同时验证 west.cn 是否放行本机 IP）"
  if bash "$INSTALL_DIR/run.sh"; then
    D="$(TZ=Asia/Shanghai date +%F)"
    if [ -f "$INSTALL_DIR/reports/$D.md" ]; then
      say "成功！报告：$INSTALL_DIR/reports/$D.md"
    else
      warn "跑完了但没生成今天的报告，看日志：$INSTALL_DIR/logs/$D.log"
    fi
  else
    warn "首跑有异常，看日志：$INSTALL_DIR/logs/$(TZ=Asia/Shanghai date +%F).log"
  fi
fi

cat <<EOF

────────────────────────────────────────────────────────────
  安装完成
────────────────────────────────────────────────────────────
  管理面板  ${WRAPPER:-python3 $INSTALL_DIR/manage.py}

            d  立即运行一次      n  设定推送方式
            e  设定通知邮箱      t  设定每天启动时间
            m  发送测试推送      r  查看最近报告
            l  查看最近日志      p  暂停/启用定时
            u  完整卸载

  目录      $INSTALL_DIR
  手动跑    bash $INSTALL_DIR/run.sh
  看日志    tail -f $INSTALL_DIR/logs/\$(date +%F).log

  ⚠️ 一天只跑一次。单次约 30 个请求是安全线，当天重复全量跑容易触发
     west.cn 限流（表现为日志里出现「查询太频繁 / code=500」）。
────────────────────────────────────────────────────────────
EOF
