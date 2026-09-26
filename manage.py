#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""过期域名雷达 · 管理面板

交互式：
    python3 manage.py            # 或安装后的短命令 wdradar
命令行（不进菜单，适合脚本调用）：
    python3 manage.py status
    python3 manage.py run
    python3 manage.py notify                # 交互式设定推送渠道
    python3 manage.py channels              # 看当前渠道
    python3 manage.py email you@qq.com      # 设定邮件收件人
    python3 manage.py time 09:30
    python3 manage.py test                  # 发一条测试推送
    python3 manage.py pause | resume
    python3 manage.py uninstall [-y]
"""

import argparse
import datetime as dt
import os
import re
import shutil
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(ROOT, "config.env")
REPORTS = os.path.join(ROOT, "reports")
LOGS = os.path.join(ROOT, "logs")
RUNNER = os.path.join(ROOT, "run.sh")
TOOLS = os.path.join(ROOT, "tools")
SENDER = os.path.join(TOOLS, "send_report.py")
NOTIFIER = os.path.join(TOOLS, "notify.py")
sys.path.insert(0, TOOLS)              # 让 manage.py 能 import notify / send_report
MARK = "# west-radar-daily"
CRONTAB = os.environ.get("WDRADAR_CRONTAB", "crontab")
# 把 crontab 落到本地文件（自测/沙箱用，不影响真实行为）
CRONTAB_FILE = os.environ.get("WDRADAR_CRONTAB_FILE", "")
DRY = os.environ.get("WDRADAR_DRY_RUN") == "1"
WRAPPER_NAMES = ("wdradar",)

# ─────────────────────────── 小工具 ───────────────────────────
C = {"r": "\033[0m", "b": "\033[1m", "dim": "\033[2m", "cy": "\033[36m",
     "gr": "\033[32m", "ye": "\033[33m", "re": "\033[31m"}


def c(t, k):
    return t if not sys.stdout.isatty() else C.get(k, "") + t + C["r"]


def hr(ch="─", n=62):
    print(c(ch * n, "dim"))


def dwidth(s):
    """显示宽度：CJK / 全角算 2 列"""
    import unicodedata
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in s)


def pad(s, w):
    return s + " " * max(0, w - dwidth(s))


def title(t):
    print()
    hr("═")
    print("  " + c(t, "b"))
    hr("═")


def ok(t):
    print("  " + c("✓", "gr") + " " + t)


def warn(t):
    print("  " + c("!", "ye") + " " + t)


def err(t):
    print("  " + c("✗", "re") + " " + t)


def is_interactive():
    try:
        return sys.stdin.isatty() and sys.stdout.isatty()
    except Exception:                                                # noqa: BLE001
        return False


def ask(prompt, default=""):
    tip = " [%s]" % default if default else ""
    try:
        v = input("  %s%s: " % (prompt, tip)).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return default
    return v or default


def pause():
    """等回车 —— 只在真终端里等；管道/CI 下直接返回，免得挂住"""
    if not is_interactive():
        print()
        return
    try:
        input(c("  回车返回菜单…", "dim"))
    except (EOFError, KeyboardInterrupt):
        print()


# ─────────────────────────── config.env ───────────────────────────
def read_config():
    cfg = {}
    if os.path.exists(CONFIG):
        for line in open(CONFIG, encoding="utf-8", errors="ignore"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                cfg[k.strip()] = v.strip().strip('"').strip("'")
    return cfg


def write_config(updates):
    lines = open(CONFIG, encoding="utf-8").read().split("\n") if os.path.exists(CONFIG) else []
    for k, v in updates.items():
        hit = False
        for i, l in enumerate(lines):
            if l.startswith(k + "="):
                lines[i] = "%s=%s" % (k, v)
                hit = True
                break
        if not hit:
            lines.append("%s=%s" % (k, v))
    open(CONFIG, "w", encoding="utf-8").write("\n".join(lines))
    try:
        os.chmod(CONFIG, 0o600)
    except OSError:
        pass


# ─────────────────────────── crontab ───────────────────────────
def cron_read():
    if CRONTAB_FILE:
        return open(CRONTAB_FILE, encoding="utf-8").read().split("\n") \
            if os.path.exists(CRONTAB_FILE) else []
    if DRY:
        return []
    try:
        r = subprocess.run([CRONTAB, "-l"], capture_output=True, text=True, timeout=20)
        return r.stdout.split("\n") if r.returncode == 0 else []
    except Exception:                                                # noqa: BLE001
        return []


def cron_write(lines):
    body = "\n".join(l for l in lines if l is not None)
    if not body.endswith("\n"):
        body += "\n"
    if CRONTAB_FILE:
        open(CRONTAB_FILE, "w", encoding="utf-8").write(body)
        return True
    if DRY:
        print(c("  [dry-run] 写入 crontab：", "dim"))
        for l in body.strip().split("\n"):
            print(c("      " + l, "dim"))
        return True
    try:
        r = subprocess.run([CRONTAB, "-"], input=body, capture_output=True,
                           text=True, timeout=20)
        if r.returncode != 0:
            err("写入 crontab 失败：%s" % (r.stderr or "").strip()[:200])
            return False
        return True
    except FileNotFoundError:
        err("这台机器没有 crontab 命令")
        return False


def cron_line():
    for l in cron_read():
        if MARK in l:
            return l
    return None


def cron_state():
    """返回 (状态, 小时, 分钟, None)；状态 ∈ none/on/paused/other

    ⚠️ cron 的字段顺序是「分 时」，这里统一转成 (小时, 分钟) 返回，
       别再从调用处猜顺序（曾因此把 09:30 回读成 30:09）。
    """
    l = cron_line()
    if not l:
        return "none", None, None, None
    paused = l.strip().startswith("#")
    body = l.strip().lstrip("#").strip()
    m = re.match(r"^(\d+)\s+(\d+)\s+\*\s+\*\s+\*", body)
    if not m:
        return "other", None, None, None
    minute, hour = int(m.group(1)), int(m.group(2))
    return ("paused" if paused else "on"), hour, minute, None


def local_offset_hours():
    # Linux 上如果外部设了 TZ 环境变量，localtime 需要 tzset() 才会跟着变
    # （Windows 的 Python 不支持 tzset，会一直用系统时区 —— 对 VPS 无影响）
    if hasattr(time, "tzset"):
        try:
            time.tzset()
        except Exception:                                            # noqa: BLE001
            pass
    off = dt.datetime.now().astimezone().utcoffset() or dt.timedelta()
    return off.total_seconds() / 3600.0


def bj_to_local(h, m):
    """北京时间的 h:m → 本机 cron 的 h:m"""
    total = h * 60 + m - 8 * 60 + int(round(local_offset_hours() * 60))
    total %= 24 * 60
    return total // 60, total % 60


def local_to_bj(h, m):
    total = h * 60 + m + 8 * 60 - int(round(local_offset_hours() * 60))
    total %= 24 * 60
    return total // 60, total % 60


def cron_install(h, m, python=None):
    lh, lm = bj_to_local(h, m)
    line = "%d %d * * * %s %s" % (lm, lh, RUNNER, MARK)
    lines = [l for l in cron_read() if MARK not in l and l.strip()]
    lines.append(line)
    return cron_write(lines)


def cron_remove():
    lines = [l for l in cron_read() if MARK not in l]
    while lines and not lines[-1].strip():
        lines.pop()
    return cron_write(lines)


def cron_pause():
    lines = []
    for l in cron_read():
        if MARK in l and not l.strip().startswith("#"):
            l = "#" + l
        lines.append(l)
    return cron_write(lines)


def cron_resume():
    out = []
    for l in cron_read():
        if MARK in l and l.lstrip().startswith("#"):
            l = l.lstrip()[1:]                      # 去掉我们加的那个 #
        out.append(l)
    return cron_write(out)


# ─────────────────────────── 运行 / 日志 / 报告 ───────────────────────────
def latest_report():
    if not os.path.isdir(REPORTS):
        return None
    files = [f for f in os.listdir(REPORTS) if f.endswith(".md")]
    files.sort()
    return os.path.join(REPORTS, files[-1]) if files else None


def latest_log():
    if not os.path.isdir(LOGS):
        return None
    files = [f for f in os.listdir(LOGS) if f.endswith(".log")]
    files.sort()
    return os.path.join(LOGS, files[-1]) if files else None


def act_run():
    title("立即运行一次")
    print("  抓取 west.cn（约 20~30 个请求）+ 打分 + 出报告，大概 1~3 分钟。")
    print("  日志同时写入 logs/%s.log" % dt.date.today().isoformat())
    print()
    t0 = time.time()
    try:
        subprocess.run(["bash", RUNNER], timeout=1800)
    except KeyboardInterrupt:
        warn("已中断（后台可能仍在写日志）")
        return
    except Exception as e:                                           # noqa: BLE001
        err("执行失败：%s" % e)
        pause()
        return
    print()
    rp = latest_report()
    if rp and os.path.getmtime(rp) >= t0 - 5:
        ok("完成，用时 %.0f 秒 → %s" % (time.time() - t0, rp))
    else:
        warn("跑完了，但没看到新报告。看日志：%s" % (latest_log() or "logs/"))
    pause()


def act_show_log():
    title("最近日志")
    lf = latest_log()
    if not lf:
        warn("还没有日志")
        pause()
        return
    print("  文件：%s" % lf)
    hr()
    lines = open(lf, encoding="utf-8", errors="ignore").read().split("\n")
    for l in lines[-40:]:
        print("  " + l)
    pause()


def act_show_report():
    title("最近报告")
    rp = latest_report()
    if not rp:
        warn("还没有报告，先跑一次（菜单 d）")
        pause()
        return
    print("  报告 : %s" % rp)
    js = rp[:-3] + ".json"
    if os.path.exists(js):
        print("  JSON : %s" % js)
    print("  大小 : %.1f KB" % (os.path.getsize(rp) / 1024))
    print("  时间 : %s" % dt.datetime.fromtimestamp(os.path.getmtime(rp))
          .strftime("%Y-%m-%d %H:%M:%S"))
    print()
    print("  在你自己电脑上看，可以：")
    print(c("      scp %s@主机:%s ." % (os.environ.get("USER", "user"), rp), "cy"))
    md = rp
    if os.path.exists(md):
        print()
        hr()
        print(c("  报告内容（前 40 行）", "dim"))
        hr()
        for l in open(md, encoding="utf-8", errors="ignore").read().split("\n")[:40]:
            print("  " + l)
    pause()


# ─────────────────────────── 设置 ───────────────────────────
def act_set_email(argv=None):
    title("设定通知邮箱")
    cfg = read_config()
    cur = cfg.get("MAIL_TO", "")
    print("  当前收件人：%s" % (cur or c("（未设置，报告只落盘不发信）", "ye")))
    print("  多个地址用英文逗号分隔；输入 - 表示清空（关闭发信）。")
    if argv is None and not is_interactive():
        err("非交互环境下必须给出地址：manage.py email <邮箱>（清空用 -）")
        sys.exit(2)
    val = argv if argv is not None else ask("收件邮箱", cur)
    if val == "-":
        val = ""
    val = (val or "").strip()
    write_config({"MAIL_TO": val})
    ok("收件人已设为：%s" % (val or "（空，不发信）"))
    if val and not cfg.get("SMTP_HOST"):
        print()
        warn("还没配发件服务器（SMTP），发信会失败。")
        print("  常见配置：")
        print("     QQ 邮箱    smtp.qq.com        465  ssl")
        print("     163 邮箱   smtp.163.com       465  ssl")
        print("     Gmail      smtp.gmail.com     465  ssl")
        print("     Outlook    smtp.office365.com 587  starttls")
        if argv is None and ask("现在配置发件服务器？(y/N)").lower() in ("y", "yes"):
            act_set_smtp()
            return
    if argv is None:
        pause()


def act_set_smtp(argv=None, followup=True):
    title("设定发件服务器（SMTP / 邮件渠道）")
    cfg = read_config()
    print(c("  注意：绝大多数邮箱要用「授权码 / 应用专用密码」，不是登录密码。", "ye"))
    print(c("  QQ 邮箱：mail.qq.com → 设置 → 账号与安全 →", "dim"))
    print(c("           开启 IMAP/SMTP 服务 → 手机验证 → 得到 16 位纯字母授权码", "dim"))
    print()
    mail_to = ask("收件邮箱（多个用逗号分隔）", cfg.get("MAIL_TO", ""))
    host = ask("SMTP 服务器", cfg.get("SMTP_HOST", "smtp.qq.com"))
    port = ask("端口（465=SSL / 587=STARTTLS）", cfg.get("SMTP_PORT", "465"))
    sec = ask("加密方式 ssl/starttls/none",
              cfg.get("SMTP_SECURITY", "ssl" if port == "465" else "starttls"))
    user = ask("发件邮箱（SMTP 用户名，填完整地址）", cfg.get("SMTP_USER", ""))
    pwd = ask("授权码 / 应用专用密码", "")
    if not pwd:
        pwd = cfg.get("SMTP_PASS", "")
        if pwd:
            print(c("      （沿用原有授权码）", "dim"))
    frm = ask("发件人地址（留空=用上面那个）", cfg.get("SMTP_FROM", ""))
    write_config({"MAIL_TO": mail_to, "SMTP_HOST": host, "SMTP_PORT": port,
                  "SMTP_SECURITY": sec, "SMTP_USER": user, "SMTP_PASS": pwd,
                  "SMTP_FROM": frm})
    ok("邮件渠道已保存")
    if followup and argv is None and is_interactive():
        print()
        if ask("现在发一封测试邮件验证？(Y/n)", "y").lower() in ("y", "yes"):
            act_test_push()
            return
    if followup and argv is None:
        pause()


# 推送渠道：编号 → (渠道 key, 名称, [(配置键, 提示语, 默认值)])
NOTIFY_MENU = [
    ("1", "wecom", "企业微信机器人", [
        ("WECOM_WEBHOOK", "机器人 Webhook 地址", "")]),
    ("2", "dingtalk", "钉钉机器人", [
        ("DINGTALK_WEBHOOK", "Webhook 地址", ""),
        ("DINGTALK_SECRET", "加签密钥（安全设置没用加签就留空）", "")]),
    ("3", "feishu", "飞书机器人", [
        ("FEISHU_WEBHOOK", "Webhook 地址", "")]),
    ("4", "serverchan", "Server酱（推到微信）", [
        ("SERVERCHAN_KEY", "SendKey（sct.ftqq.com 拿）", "")]),
    ("5", "pushplus", "PushPlus（推到微信）", [
        ("PUSHPLUS_TOKEN", "token（pushplus.plus 拿）", "")]),
    ("6", "telegram", "Telegram Bot", [
        ("TG_BOT_TOKEN", "Bot Token（找 @BotFather 要）", ""),
        ("TG_CHAT_ID", "你的 chat_id（找 @userinfobot 看）", "")]),
    ("7", "ntfy", "ntfy（可自建）", [
        ("NTFY_TOPIC", "topic 名（手机 App 订阅同名即可，别人猜不到就行）", ""),
        ("NTFY_SERVER", "服务器地址", "https://ntfy.sh")]),
    ("8", "webhook", "自定义 Webhook（POST JSON）", [
        ("CUSTOM_WEBHOOK", "URL", "")]),
    ("9", "email", "邮件（SMTP）", []),
]
NOTIFY_HINT = {
    "wecom": "企业微信群 → 右上角「…」→ 群机器人 → 添加 → 复制 Webhook",
    "dingtalk": "群设置 → 智能群助手 → 添加机器人 → 自定义 → 复制 Webhook",
    "feishu": "群设置 → 群机器人 → 添加机器人 → 自定义机器人 → 复制 Webhook",
    "serverchan": "浏览器开 sct.ftqq.com，微信扫码登录后拿 SendKey",
    "pushplus": "浏览器开 pushplus.plus，微信扫码登录后拿 token",
    "telegram": "①找 @BotFather 发 /newbot 拿 token ②找 @userinfobot 拿 chat_id",
    "ntfy": "手机装 ntfy App，订阅一个别人猜不到的 topic 名",
    "webhook": "任何能收 POST JSON 的地址，字段：title/content/source/date",
    "email": "QQ/163/Gmail 都行，用「授权码」；海外 VPS 别用 25 端口",
}


def put_first(cfg, chan):
    cur = [c.strip() for c in (cfg.get("NOTIFY_CHANNELS") or "").split(",") if c.strip()]
    write_config({"NOTIFY_CHANNELS": ",".join([chan] + [c for c in cur if c != chan])})


def act_set_notify():
    title("设定推送方式")
    cfg = read_config()
    import notify
    cur = notify.configured_channels(cfg)
    print("  当前渠道（按尝试顺序）：%s"
          % ("、".join(cur) if cur else c("（无，报告只落盘）", "ye")))
    print(c("  配好多个渠道会自动失败转移：第一个成功就停。", "dim"))
    print()
    for num, chan, name, _ in NOTIFY_MENU:
        mark = " " + c("←已启用", "gr") if chan in cur else ""
        print("   %s) %s%s" % (c(num, "cy"), pad(name, 24), mark))
    print("   " + c("0", "cy") + ") 返回")
    print()
    print(c("  💡 海外 VPS 上 SMTP 常被机房封，建议首选「1) 企业微信机器人」——", "dim"))
    print(c("     它就是一次 HTTPS POST，最稳。", "dim"))
    hr()
    ch = input("  请选择渠道: ").strip()
    if ch in ("0", ""):
        return
    hit = next((x for x in NOTIFY_MENU if x[0] == ch), None)
    if not hit:
        err("没有这个选项")
        pause()
        return
    _, chan, name, fields = hit
    title("设定：%s" % name)
    print(c("  %s" % NOTIFY_HINT.get(chan, ""), "dim"))
    print()
    if chan == "email":
        act_set_smtp(followup=True)
        put_first(read_config(), "email")
        return
    vals = {}
    for key, prompt, dflt in fields:
        old = cfg.get(key, "") or dflt
        v = ask(prompt, old)
        vals[key] = v
    write_config(vals)
    put_first(read_config(), chan)
    ok("%s 已保存，并设为**首选渠道**" % name)
    print(c("     渠道顺序：%s" % read_config().get("NOTIFY_CHANNELS"), "dim"))
    print()
    if ask("现在发一条测试推送验证？(Y/n)", "y").lower() in ("y", "yes"):
        act_test_push()
        return
    pause()


def act_test_push():
    title("发送测试推送")
    cfg = read_config()
    import notify
    chans = notify.configured_channels(cfg)
    if not chans:
        warn("还没设定任何推送渠道，先用菜单 n")
        pause()
        return
    print("  渠道顺序：%s" % "、".join(chans))
    print()
    try:
        r = subprocess.run([sys.executable, NOTIFIER, "--test"],
                           capture_output=True, text=True, timeout=180)
    except Exception as e:                                           # noqa: BLE001
        err("调用失败：%s" % e)
        pause()
        return
    out = ((r.stdout or "") + (r.stderr or "")).strip()
    for l in out.split("\n"):
        print("  " + l)
    print()
    if r.returncode == 0:
        ok("测试推送成功")
        print(c("  没收到？检查：渠道参数对不对 / 机器人是否被移出群 / 手机是否订阅了该 topic。", "dim"))
    else:
        err("全部渠道都失败了（上面有每个渠道的具体报错）")
        print()
        print("  常见原因：")
        print("    · 企业微信/钉钉：Webhook 地址不完整，或机器人已被移出群")
        print("    · 钉钉开了「加签」但没填密钥 → 菜单 n 里补上 DINGTALK_SECRET")
        print("    · Telegram：没先给 bot 发过一条消息（bot 不能主动找陌生人）")
        print("    · 邮件：授权码错 / 机房封了 465、587 端口 / 境外 IP 被当风险登录")
    pause()


def act_set_time(argv=None):
    title("设定每天启动时间（北京时间）")
    st, hour, minute, _ = cron_state()
    if st in ("on", "paused") and hour is not None:
        bh, bm = local_to_bj(hour, minute)
        print("  当前：北京时间 %02d:%02d（本机 cron %02d:%02d）" % (bh, bm, hour, minute))
    else:
        print("  当前：%s" % ("未设置定时任务" if st == "none" else st))
    off = local_offset_hours()
    print(c("  本机时区 UTC%+g；会自动换算成 cron 时间。", "dim"))
    print()
    if argv is None and not is_interactive():
        err("非交互环境下必须给出时间：manage.py time 09:30")
        sys.exit(2)
    val = argv if argv is not None else ask("几点跑（如 9 / 09:30）", "09:00")
    m = re.match(r"^\s*(\d{1,2})\s*[:：]?\s*(\d{0,2})\s*$", val or "")
    if not m:
        err("格式不对，示例：9 或 09:30")
        pause()
        return
    h, mi = int(m.group(1)), int(m.group(2) or 0)
    if not (0 <= h <= 23 and 0 <= mi <= 59):
        err("时间超出范围")
        pause()
        return
    if cron_install(h, mi):
        lh, lm = bj_to_local(h, mi)
        ok("已设定：每天北京时间 %02d:%02d 运行（本机 cron %02d:%02d，UTC%+g）"
           % (h, mi, lh, lm, off))
        if off != 8:
            print(c("     换算说明：本机比北京时间慢 %.0f 小时" % (8 - off)
                    if off < 8 else "     换算说明：本机比北京时间快 %.0f 小时" % (off - 8), "dim"))
    if argv is None:
        pause()


def act_toggle(argv=None):
    title("暂停 / 启用定时任务")
    st, hour, minute, _ = cron_state()
    if st == "none":
        warn("还没有定时任务，先用菜单 t 设定时间")
        pause()
        return
    if argv in ("pause", "resume"):
        target = argv
    else:
        print("  当前状态：%s" % ("已暂停" if st == "paused" else "运行中"))
        target = "pause" if st == "on" else "resume"
        if ask("要%s吗？(Y/n)" % ("暂停" if target == "pause" else "启用"), "y").lower() not in ("y", "yes"):
            return
    if target == "pause":
        cron_pause()
        ok("已暂停（crontab 里那行被注释掉，配置都保留）")
    else:
        cron_resume()
        ok("已启用")
    if argv is None:
        pause()




# ─────────────────────────── 卸载 ───────────────────────────
def wrapper_paths():
    out = []
    for d in ("/usr/local/bin", os.path.join(os.path.expanduser("~"), ".local", "bin")):
        for n in WRAPPER_NAMES:
            p = os.path.join(d, n)
            if os.path.exists(p) or os.path.islink(p):
                out.append(p)
    return out


def act_uninstall(argv=None):
    title("完整卸载")
    print("  将会做这些事：")
    print("    1) 删掉定时任务（crontab 里 %s 那行）" % MARK)
    print("    2) 删掉短命令 wdradar")
    print("    3) 删掉整个安装目录 %s（含全部报告与日志）" % ROOT)
    print()
    print(c("  ⚠️ 第 3 步不可恢复。", "ye"))
    if argv is not True:
        if not is_interactive():
            err("非交互环境下必须显式确认：manage.py uninstall -y")
            sys.exit(2)
        if ask("确认卸载？输入 yes 继续").lower() not in ("yes", "y"):
            warn("已取消")
            pause()
            return
    cron_remove()
    ok("定时任务已移除")
    for p in wrapper_paths():
        try:
            os.remove(p)
            ok("已删除 %s" % p)
        except OSError as e:
            warn("删除 %s 失败：%s" % (p, e))
    ok("配置与报告即将随目录一起删除")
    if DRY:
        print(c("  [dry-run] 跳过删除目录 %s" % ROOT, "dim"))
        return
    print()
    print("  正在删除 %s …" % ROOT)
    os.chdir("/tmp")
    try:
        shutil.rmtree(ROOT)
        print()
        ok("已完整卸载，再见。")
    except OSError as e:
        err("删除失败：%s" % e)
        print("  请手动执行： rm -rf %s" % ROOT)
    sys.exit(0)


def act_channels(argv=None):
    import notify
    cfg = read_config()
    title("推送渠道")
    chans = notify.configured_channels(cfg)
    print("  当前配置：%s" % ("、".join(chans) if chans else c("（无，报告只落盘）", "ye")))
    print("  说明：按这个顺序尝试，第一个成功就停（= 自动失败转移）。")
    print()
    for ch in notify.CHANNEL_ORDER:
        print("   %-11s %s%s" % (ch, notify.CHANNEL_HELP[ch],
                                c("   ←已启用", "gr") if ch in chans else ""))
    if argv is None:
        pause()


# ─────────────────────────── 状态 ───────────────────────────
def act_status(argv=None):
    cfg = read_config()
    st, hour, minute, _ = cron_state()
    off = local_offset_hours()
    title("当前状态")
    if st in ("on", "paused") and hour is not None:
        bh, bm = local_to_bj(hour, minute)
        tag = c("● 运行中", "gr") if st == "on" else c("❚❚ 已暂停", "ye")
        print("  定时任务 : %s 每天 %02d:%02d（北京时间）" % (tag, bh, bm))
        print("             %s本机 cron %02d:%02d，UTC%+g" % (" " * 12, hour, minute, off))
    elif st == "paused":
        print("  定时任务 : %s" % c("❚❚ 已暂停（那行 cron 已被注释）", "ye"))
    else:
        print("  定时任务 : %s" % c("未设置", "ye"))
    import notify
    chans = notify.configured_channels(cfg)
    print("  推送渠道 : %s" % ("、".join(chans) if chans
                              else c("未配置（报告只落盘）", "ye")))
    for ch in chans:
        tail = ""
        if ch == "wecom" and cfg.get("WECOM_WEBHOOK"):
            tail = "  %s…" % cfg["WECOM_WEBHOOK"][-12:]
        elif ch == "email":
            tail = "  → %s" % (cfg.get("MAIL_TO") or "（收件人未设）")
        elif ch == "telegram":
            tail = "  chat %s" % (cfg.get("TG_CHAT_ID") or "?")
        elif ch == "ntfy":
            tail = "  topic %s" % (cfg.get("NTFY_TOPIC") or "?")
        print("             %-11s%s" % (ch, c(tail, "dim")))
    if cfg.get("SMTP_HOST"):
        print("  发件服务器: %s:%s (%s)" % (cfg.get("SMTP_HOST"), cfg.get("SMTP_PORT"),
                                            cfg.get("SMTP_SECURITY")))
    rp = latest_report()
    print("  最近报告 : %s" % (os.path.basename(rp) if rp else "还没有"))
    lf = latest_log()
    print("  最近日志 : %s" % (os.path.basename(lf) if lf else "还没有"))
    print("  安装目录 : %s" % ROOT)
    n = len(os.listdir(REPORTS)) if os.path.isdir(REPORTS) else 0
    print("  报告数量 : %d 个文件（保留 60 天）" % n)
    if argv is None:
        pause()


# ─────────────────────────── 菜单 ───────────────────────────
MENU = [
    ("d", "立即运行一次", "马上抓取并出报告（不等定时）", act_run),
    ("n", "设定推送方式", "企业微信 / 钉钉 / 飞书 / 邮件 / Telegram …", act_set_notify),
    ("e", "设定通知邮箱", "邮件渠道的收件地址", act_set_email),
    ("t", "设定每天启动时间", "北京时间几点跑", act_set_time),
    ("m", "发送测试推送", "验证推送渠道是否配通", act_test_push),
    ("r", "查看最近报告", "路径 + 摘要", act_show_report),
    ("l", "查看最近日志", "排查失败原因", act_show_log),
    ("p", "暂停 / 启用定时", "临时停跑，配置保留", act_toggle),
    ("u", "完整卸载", "删定时任务 + 短命令 + 整个目录", act_uninstall),
]


def clear_screen():
    """只在真终端里清屏 —— 非 tty（管道/CI）下不要白白去起一个 shell"""
    if not sys.stdout.isatty():
        return
    os.system("cls" if os.name == "nt" else "clear")


def menu():
    while True:
        clear_screen()
        cfg = read_config()
        st, hour, minute, _ = cron_state()
        print()
        hr("═")
        print("  " + c("过期域名雷达 · 管理面板", "b"))
        hr("═")
        if st in ("on", "paused") and hour is not None:
            bh, bm = local_to_bj(hour, minute)
            stat = ((c("运行中", "gr") if st == "on" else c("已暂停", "ye"))
                    + " 每天 %02d:%02d（北京时间）" % (bh, bm))
        elif st == "paused":
            stat = c("已暂停", "ye")
        else:
            stat = c("未设置定时", "ye")
        print("   状态   %s" % stat)
        try:
            import notify
            chans = notify.configured_channels(cfg)
        except Exception:                                            # noqa: BLE001
            chans = []
        print("   推送   %s" % ("、".join(chans) if chans else c("未配置（只落盘）", "ye")))
        rp = latest_report()
        print("   报告   %s" % (os.path.basename(rp) if rp else c("还没有，按 d 跑一次", "dim")))
        hr()
        for i, (k, name, desc, _) in enumerate(MENU, 1):
            print("   %s) %s %s" % (c(k, "re" if k == "u" else "cy"), pad(name, 18),
                                    c(desc, "dim")))
            if i == 4:
                hr("·")
        print("    %s) %s" % (c("q", "cy"), "退出"))
        hr()
        try:
            ch = input("  请选择: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if ch in ("q", "quit", "exit", ""):
            return
        hit = None
        for k, name, desc, fn in MENU:
            if ch == k or ch == name:
                hit = fn
                break
        if ch.isdigit() and 1 <= int(ch) <= len(MENU):
            hit = MENU[int(ch) - 1][3]
        if not hit:
            warn("没有这个选项：%s" % ch)
            time.sleep(1)
            continue
        try:
            hit()
        except KeyboardInterrupt:
            print()
            warn("已取消")
            time.sleep(1)


CMDS = {
    "status": lambda a: act_status(a),
    "run": lambda a: act_run(),
    "notify": lambda a: act_set_notify(),
    "email": lambda a: act_set_email(a),
    "time": lambda a: act_set_time(a),
    "test": lambda a: act_test_push(),
    "test-mail": lambda a: act_test_push(),
    "channels": lambda a: act_channels(),
    "report": lambda a: act_show_report(),
    "log": lambda a: act_show_log(),
    "pause": lambda a: act_toggle("pause"),
    "resume": lambda a: act_toggle("resume"),
    "uninstall": lambda a: act_uninstall(a),
}


def main():
    argv = sys.argv[1:]
    if not argv:
        menu()
        return
    cmd = argv[0].lower()
    if cmd in ("-h", "--help", "help"):
        print(__doc__)
        return
    if cmd not in CMDS:
        err("未知命令：%s" % cmd)
        print(__doc__)
        sys.exit(2)
    if cmd == "uninstall":
        CMDS[cmd]("-y" in argv)
    elif cmd in ("email", "time"):
        # 原样传参（别过滤以 - 开头的参数：`email -` 里的 "-" 是"清空"的意思）
        rest = argv[1:]
        CMDS[cmd](rest[0] if rest else None)
    else:
        CMDS[cmd](None)


if __name__ == "__main__":
    main()
