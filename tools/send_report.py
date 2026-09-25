#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把当天的雷达报告通过 SMTP 发出去（VPS 上用，不依赖 WorkBuddy）。

用法：
    python3 send_report.py --date 2026-09-25
    python3 send_report.py --date 2026-09-25 --failure     # 抓取失败时的告知邮件

配置读同目录的 config.env（KEY=VALUE），需要的键：
    MAIL_TO=you@example.com            # 收件人，多个用逗号分隔；留空则跳过发信
    SMTP_HOST=smtp.qq.com
    SMTP_PORT=465
    SMTP_SECURITY=ssl                  # ssl | starttls | none
    SMTP_USER=you@qq.com
    SMTP_PASS=授权码                    # 注意：多数邮箱要用"授权码/应用专用密码"，不是登录密码
    SMTP_FROM=                         # 留空则用 SMTP_USER
    MAIL_ON_FAILURE=1                  # 抓取失败时也发一封说明（默认 1）
"""

import argparse
import datetime as dt
import json
import os
import re
import smtplib
import ssl
import sys
from email.message import EmailMessage

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPORTS = os.path.join(ROOT, "reports")
CONFIG = os.path.join(ROOT, "config.env")


def load_config(path=CONFIG):
    cfg = {}
    if not os.path.exists(path):
        return cfg
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            v = v.strip().strip('"').strip("'")
            cfg[k.strip()] = v
    return cfg


def fmt_row(i, it):
    bits = [it.get("cls_label") or "", "%s 分" % it.get("score")]
    if it.get("regdate"):
        bits.append("注册 %s" % it["regdate"])
    if it.get("premium"):
        bits.append("⚠️溢价 %s 元" % (it.get("premiumprice") or "?"))
    if it.get("hot"):
        bits.append("⚠️已被预订→竞拍")
    tips = ", ".join(x for x in (it.get("reasons") or []) if x)
    line = "%2d. %-16s %s" % (i, it.get("domain", "?"), " ｜ ".join(bits))
    if tips:
        line += "\n      %s" % tips
    return line


def build_body(result, date_str, failure=None):
    if failure:
        return (
            "过期域名雷达 · %s\n"
            "────────────────────────\n\n"
            "今天没有取到数据，未生成日报。\n\n"
            "原因：%s\n\n"
            "按设计，这种情况不会发出空报告、也不会编造域名。\n"
            "常见原因与处理：\n"
            "  · west.cn 限流（code=500 查询太频繁）→ 换一个出口 IP，或隔几小时重试；\n"
            "  · 当天被跑了两次全量抓取 → 保持每天只跑一次（单次约 30 次请求是安全线）；\n"
            "  · 服务器所在网络访问 west.cn 异常 → 检查网络。\n\n"
            "详细日志见 VPS 上 logs/%s.log。\n" % (date_str, date_str, date_str)
        )

    top = result.get("top10") or []
    lines = ["过期域名雷达 · %s" % date_str, "─" * 40, "", "【最值得关注 TOP10】", ""]
    for i, it in enumerate(top, 1):
        lines.append(fmt_row(i, it))

    pool = result.get("pool_totals") or {}
    if pool:
        lines += ["", "池内总量：" + "、".join("%s %s 条" % (k, v) for k, v in sorted(pool.items()))]
    cls = result.get("by_class") or {}
    if cls:
        lines.append("值得关注：" + "、".join("%s %s 个" % (k, v) for k, v in cls.items() if v))

    warns = []
    for it in result.get("all_items") or []:
        if it.get("hot"):
            warns.append("  · %s 已有人预订 → 会进竞拍，价格可能被抬高" % it["domain"])
        elif it.get("premium"):
            warns.append("  · %s 溢价域名 → 预订需额外支付 %s 元"
                         % (it["domain"], it.get("premiumprice") or "?"))
    warns = warns[:12]
    if warns:
        lines += ["", "【费用提示】"] + warns

    watch = result.get("watch") or []
    if watch:
        lines += ["", "【定向核查】"]
        for w in watch:
            st = "已被预订" if w.get("isyuding") else "仍可预订"
            if w.get("premium"):
                st += "，溢价 %s 元" % (w.get("premiumprice") or "?")
            lines.append("  · %-16s 删除日期 %s ｜ %s"
                         % (w.get("domain"), w.get("deldate") or "—", st))

    note = (result.get("sample_note") or "").strip()
    if note:
        clean = re.sub(r"[*`_#>]", "", note).strip()
        lines += ["", "【说明】", clean]

    lines += ["", "─" * 40,
              "完整报告见附件 HTML（含全部候选明细）。",
              "生成时间 %s" % (result.get("generated_at") or "")]

    return "\n".join(x for x in lines if x is not None)


def send(cfg, subject, body, attach):
    host = cfg.get("SMTP_HOST")
    if not host:
        print("未配置 SMTP_HOST，跳过发信（报告已落盘）")
        return False
    to = [x.strip() for x in (cfg.get("MAIL_TO") or "").split(",") if x.strip()]
    if not to:
        print("未配置 MAIL_TO，跳过发信（报告已落盘）")
        return False

    port = int(cfg.get("SMTP_PORT") or 465)
    sec = (cfg.get("SMTP_SECURITY") or ("ssl" if port == 465 else "starttls")).lower()
    user = cfg.get("SMTP_USER") or ""
    pwd = cfg.get("SMTP_PASS") or ""
    sender = cfg.get("SMTP_FROM") or user

    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = ", ".join(to)
    msg["Subject"] = subject
    msg.set_content(body)
    msg.add_alternative(
        "<pre style=\"font:13px/1.6 Consolas,Menlo,monospace;white-space:pre-wrap\">%s</pre>"
        % body.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"),
        subtype="html")

    for path in attach:
        if not os.path.exists(path):
            continue
        ctype = "text/html" if path.endswith(".html") else (
            "text/markdown" if path.endswith(".md") else "application/octet-stream")
        maintype, _, subtype = ctype.partition("/")
        with open(path, "rb") as f:
            msg.add_attachment(f.read(), maintype=maintype, subtype=subtype,
                               filename=os.path.basename(path))

    ctx = ssl.create_default_context()
    if sec == "ssl":
        srv = smtplib.SMTP_SSL(host, port, timeout=60, context=ctx)
    else:
        srv = smtplib.SMTP(host, port, timeout=60)
    try:
        if sec == "starttls":
            srv.starttls(context=ctx)
        if user:
            srv.login(user, pwd)
        srv.send_message(msg)
    finally:
        try:
            srv.quit()
        except Exception:
            pass
    print("已发送到 %s（附件 %s）" % (", ".join(to), "、".join(os.path.basename(p) for p in attach)))
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=dt.date.today().isoformat())
    ap.add_argument("--failure", action="store_true", help="发送抓取失败告知邮件")
    ap.add_argument("--reason", default="west.cn 限流或抓取结果为空（详见日志）")
    ap.add_argument("--subject-prefix", default="过期域名雷达日报")
    args = ap.parse_args()

    cfg = load_config()
    date_str = args.date

    if args.failure:
        if (cfg.get("MAIL_ON_FAILURE", "1") or "1") == "0":
            print("MAIL_ON_FAILURE=0，不发失败告知")
            return
        body = build_body(None, date_str, failure=args.reason)
        send(cfg, "[%s] %s · 未取到数据" % (args.subject_prefix, date_str), body, [])
        return

    jpath = os.path.join(REPORTS, "%s.json" % date_str)
    if not os.path.exists(jpath):
        print("找不到 %s，无法发信" % jpath, file=sys.stderr)
        sys.exit(2)
    with open(jpath, "r", encoding="utf-8") as f:
        result = json.load(f)

    body = build_body(result, date_str)
    attach = [os.path.join(REPORTS, "%s.html" % date_str),
              os.path.join(REPORTS, "%s.md" % date_str)]
    ok = send(cfg, "[%s] %s" % (args.subject_prefix, date_str), body, attach)
    sys.exit(0 if ok or not cfg.get("SMTP_HOST") else 1)


if __name__ == "__main__":
    main()
