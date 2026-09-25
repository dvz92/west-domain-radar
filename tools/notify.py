#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把当天的雷达结果推送出去 —— 支持多渠道，按顺序自动失败转移。

为什么不止要邮件：海外 VPS 上 SMTP 经常不通（机房封 25/465/587 出站，
或收件方把境外 IP 当风险登录拒掉）。而企业微信/钉钉/飞书这类
「机器人 Webhook」就是一次 HTTPS POST，最稳、最省事。

用法：
    python3 notify.py --date 2026-09-26            # 推送当天报告
    python3 notify.py --test                       # 发一条测试消息
    python3 notify.py --date 2026-09-26 --failure   # 告知"今天没取到数据"
    python3 notify.py --list                       # 看当前配置了哪些渠道

配置（config.env）：
    NOTIFY_CHANNELS=wecom,email     # 按顺序尝试，第一个成功就停（=自动失败转移）
    各渠道参数见 CHANNEL_HELP
"""

import argparse
import base64
import datetime as dt
import hashlib
import hmac
import json
import os
import sys
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
REPORTS = os.path.join(ROOT, "reports")
CONFIG = os.path.join(ROOT, "config.env")
sys.path.insert(0, HERE)

UA = "west-domain-radar/1.0"

CHANNEL_HELP = {
    "wecom": "企业微信机器人 Webhook（最稳，强烈推荐）",
    "dingtalk": "钉钉机器人 Webhook（支持加签）",
    "feishu": "飞书机器人 Webhook",
    "serverchan": "Server酱（推到微信）",
    "pushplus": "PushPlus（推到微信）",
    "telegram": "Telegram Bot（海外 VPS 很好用）",
    "ntfy": "ntfy（可自建，无需注册）",
    "webhook": "自定义 Webhook（POST JSON）",
    "email": "邮件（SMTP）",
}
CHANNEL_ORDER = list(CHANNEL_HELP)


# ───────────────────────────── 配置 ─────────────────────────────
def load_config(path=CONFIG):
    cfg = {}
    if os.path.exists(path):
        for line in open(path, encoding="utf-8", errors="ignore"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                cfg[k.strip()] = v.strip().strip('"').strip("'")
    return cfg


# 每个渠道需要哪些参数才算"配好了"
REQUIRED = {
    "wecom": ("WECOM_WEBHOOK",),
    "dingtalk": ("DINGTALK_WEBHOOK",),
    "feishu": ("FEISHU_WEBHOOK",),
    "serverchan": ("SERVERCHAN_KEY",),
    "pushplus": ("PUSHPLUS_TOKEN",),
    "telegram": ("TG_BOT_TOKEN", "TG_CHAT_ID"),
    "ntfy": ("NTFY_TOPIC",),
    "webhook": ("CUSTOM_WEBHOOK",),
    "email": ("SMTP_HOST", "MAIL_TO"),
}


def missing_params(cfg, chan):
    return [k for k in REQUIRED.get(chan, ()) if not cfg.get(k)]


def configured_channels(cfg):
    """**真正可用的**渠道列表。

    ⚠️ 只认「参数完整填好了」的渠道 —— 光在 NOTIFY_CHANNELS 里写个名字不算，
       否则状态页会谎报"已启用"，测试时才发现根本没配。
    """
    raw = cfg.get("NOTIFY_CHANNELS") or ""
    wanted = [c.strip().lower() for c in raw.split(",") if c.strip()]
    if not wanted:
        wanted = list(CHANNEL_ORDER)                 # 没显式指定 → 全部按顺序看谁配好了
    return [c for c in wanted if c in CHANNEL_HELP and not missing_params(cfg, c)]


# ───────────────────────────── 正文 ─────────────────────────────
def build_text(result, date_str, failure=None):
    """正文。Markdown 渠道直接用；Telegram 会包成 <pre> 转义后发送。"""
    if failure:
        return ("过期域名雷达 · %s\n\n"
                "今天没取到数据，未生成日报。\n\n"
                "原因：%s\n\n"
                "按设计：这种情况不发空报告、不编造域名。\n"
                "常见处理：换个出口 IP、或隔几小时重试；保持每天只跑一次。\n"
                "详细日志见 VPS 上 logs/%s.log" % (date_str, failure, date_str))

    lines = ["**过期域名雷达 · %s**" % date_str, ""]
    top = result.get("top10") or []
    if not top:
        lines.append("（本轮没有筛出值得关注的域名）")
    for i, it in enumerate(top, 1):
        seg = ["%d. **%s**" % (i, it.get("domain", "?")),
               str(it.get("cls_label") or ""),
               "%s 分" % it.get("score")]
        if it.get("regdate"):
            seg.append("注册 %s" % it["regdate"])
        if it.get("premium"):
            seg.append("⚠️溢价 %s 元" % (it.get("premiumprice") or "?"))
        elif it.get("hot"):
            seg.append("⚠️已被预订→竞拍")
        lines.append(" ｜ ".join(x for x in seg if x))
        reason = "、".join(x for x in (it.get("reasons") or []) if x)
        if reason:
            lines.append("     %s" % reason)

    pool = result.get("pool_totals") or {}
    if pool:
        lines += ["", "池内：" + "、".join("%s %s 条" % (k, v) for k, v in sorted(pool.items()))]
    cls = result.get("by_class") or {}
    got = "、".join("%s %s" % (k, v) for k, v in cls.items() if v)
    if got:
        lines.append("值得关注：" + got)

    watch = result.get("watch") or []
    if watch:
        lines += ["", "**定向核查**"]
        for w in watch:
            st = "已被预订" if w.get("isyuding") else "仍可预订"
            lines.append("  · %s（%s，%s）" % (w.get("domain"), w.get("deldate") or "—", st))

    warn = []
    for it in result.get("all_items") or []:
        if it.get("hot"):
            warn.append("  · %s 已有人预订 → 会进竞拍" % it["domain"])
        elif it.get("premium"):
            warn.append("  · %s 溢价域名（%s 元）" % (it["domain"], it.get("premiumprice") or "?"))
    if warn:
        lines += ["", "**费用提示**"] + warn[:8]

    lines += ["", "完整 HTML 报告在 VPS：reports/%s.html" % date_str]
    return "\n".join(lines)


def build_title(date_str, failure=False):
    if failure:
        return "[过期域名雷达] %s · 未取到数据" % date_str
    return "过期域名雷达日报 · %s" % date_str


# ───────────────────────────── HTTP ─────────────────────────────
def post_json(url, payload, timeout=30):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers={
        "Content-Type": "application/json; charset=utf-8", "User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read().decode("utf-8", "replace")


def post_text(url, text, timeout=30, headers=None):
    h = {"Content-Type": "text/plain; charset=utf-8", "User-Agent": UA}
    h.update(headers or {})
    req = urllib.request.Request(url, data=text.encode("utf-8"), headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read().decode("utf-8", "replace")


def esc_html(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def ascii_header(s):
    """HTTP 头只能是 latin-1；含有中文时用 RFC 2047 编码，实在不行就返回空串"""
    try:
        s.encode("latin-1")
        return s
    except UnicodeEncodeError:
        try:
            return "=?UTF-8?B?%s?=" % base64.b64encode(s.encode("utf-8")).decode("ascii")
        except Exception:                                            # noqa: BLE001
            return ""


# ───────────────────────────── 各渠道 ─────────────────────────────
def ch_wecom(cfg, title, text, result=None, attach=(), reason="", **kw):
    hook = cfg.get("WECOM_WEBHOOK")
    if not hook:
        raise RuntimeError("WECOM_WEBHOOK 未配置")
    content = text                       # 企业微信 markdown 上限 4096 字节
    if len(content.encode("utf-8")) > 4000:
        while len(content.encode("utf-8")) > 3900 and len(content) > 200:
            content = content[:int(len(content) * 0.9)]
        content += "\n\n（内容过长已截断，完整报告见 VPS）"
    _, body = post_json(hook, {"msgtype": "markdown", "markdown": {"content": content}})
    j = json.loads(body or "{}")
    if j.get("errcode") not in (0, None):
        raise RuntimeError("errcode=%s %s" % (j.get("errcode"), j.get("errmsg")))
    return "企业微信机器人"


def ch_dingtalk(cfg, title, text, result=None, attach=(), reason="", **kw):
    base = cfg.get("DINGTALK_WEBHOOK")
    if not base:
        raise RuntimeError("DINGTALK_WEBHOOK 未配置")
    url = base
    secret = cfg.get("DINGTALK_SECRET") or ""
    if secret:                                     # 加签模式
        ts = str(int(dt.datetime.now().timestamp() * 1000))
        digest = hmac.new(secret.encode("utf-8"),
                          ("%s\n%s" % (ts, secret)).encode("utf-8"),
                          hashlib.sha256).digest()
        sign = urllib.parse.quote_plus(base64.b64encode(digest).decode("utf-8"))
        url = "%s%s&timestamp=%s&sign=%s" % (base, "&" if "?" in base else "?", ts, sign)
    _, body = post_json(url, {"msgtype": "markdown",
                             "markdown": {"title": title, "text": text}})
    j = json.loads(body or "{}")
    if j.get("errcode") not in (0, None):
        raise RuntimeError("errcode=%s %s" % (j.get("errcode"), j.get("errmsg")))
    return "钉钉机器人"


def ch_feishu(cfg, title, text, result=None, attach=(), reason="", **kw):
    hook = cfg.get("FEISHU_WEBHOOK")
    if not hook:
        raise RuntimeError("FEISHU_WEBHOOK 未配置")
    _, body = post_json(hook, {"msg_type": "text",
                              "content": {"text": "%s\n\n%s" % (title, text)}})
    j = json.loads(body or "{}")
    code = j.get("code", j.get("StatusCode", 0))
    if code not in (0, None):
        raise RuntimeError("code=%s %s" % (code, body[:160]))
    return "飞书机器人"


def ch_serverchan(cfg, title, text, result=None, attach=(), reason="", **kw):
    key = cfg.get("SERVERCHAN_KEY")
    if not key:
        raise RuntimeError("SERVERCHAN_KEY 未配置")
    _, body = post_json("https://sctapi.ftqq.com/%s.send" % key,
                        {"title": title, "desp": text})
    j = json.loads(body or "{}")
    if j.get("code") not in (0, None):
        raise RuntimeError("%s" % body[:200])
    return "Server酱"


def ch_pushplus(cfg, title, text, result=None, attach=(), reason="", **kw):
    token = cfg.get("PUSHPLUS_TOKEN")
    if not token:
        raise RuntimeError("PUSHPLUS_TOKEN 未配置")
    _, body = post_json("https://www.pushplus.plus/send",
                        {"token": token, "title": title, "content": text,
                         "template": "markdown"})
    j = json.loads(body or "{}")
    if j.get("code") not in (200, 0, None):
        raise RuntimeError("%s" % body[:200])
    return "PushPlus"


def ch_telegram(cfg, title, text, result=None, attach=(), reason="", **kw):
    tok, chat = cfg.get("TG_BOT_TOKEN"), cfg.get("TG_CHAT_ID")
    if not (tok and chat):
        raise RuntimeError("TG_BOT_TOKEN / TG_CHAT_ID 未配置")
    body = "<b>%s</b>\n\n<pre>%s</pre>" % (esc_html(title), esc_html(text))
    if len(body.encode("utf-8")) > 3800:
        body = "<b>%s</b>\n\n<pre>%s</pre>" % (esc_html(title), esc_html(text[:2400]))
    _, out = post_json("https://api.telegram.org/bot%s/sendMessage" % tok,
                       {"chat_id": chat, "text": body, "parse_mode": "HTML",
                        "disable_web_page_preview": True})
    j = json.loads(out or "{}")
    if not j.get("ok"):
        raise RuntimeError("%s" % out[:200])
    return "Telegram"


def ch_ntfy(cfg, title, text, result=None, attach=(), reason="", **kw):
    topic = cfg.get("NTFY_TOPIC")
    if not topic:
        raise RuntimeError("NTFY_TOPIC 未配置")
    server = (cfg.get("NTFY_SERVER") or "https://ntfy.sh").rstrip("/")
    h = {"Tags": "globe"}
    t = ascii_header(title)                  # Header 不能直接放中文
    if t:
        h["Title"] = t
    post_text("%s/%s" % (server, topic), text, headers=h)
    return "ntfy（%s）" % server


def ch_webhook(cfg, title, text, result=None, attach=(), reason="", **kw):
    url = cfg.get("CUSTOM_WEBHOOK")
    if not url:
        raise RuntimeError("CUSTOM_WEBHOOK 未配置")
    st, body = post_json(url, {"title": title, "content": text,
                              "source": "west-domain-radar",
                              "date": (result or {}).get("date")})
    if st >= 400:
        raise RuntimeError("HTTP %s：%s" % (st, body[:160]))
    return "自定义 Webhook"


def ch_email(cfg, title, text, result=None, attach=(), reason="", **kw):
    if not (cfg.get("SMTP_HOST") and cfg.get("MAIL_TO")):
        raise RuntimeError("SMTP_HOST / MAIL_TO 未配置")
    import send_report
    body = send_report.build_body(result, (result or {}).get("date") or "",
                                  failure=None if result else (reason or "见日志"))
    if not send_report.send(cfg, title, body, list(attach)):
        raise RuntimeError("邮件未发出（检查授权码 / 端口是否被机房封）")
    return "邮件（%s）" % cfg.get("MAIL_TO")


CHANNELS = {"wecom": ch_wecom, "dingtalk": ch_dingtalk, "feishu": ch_feishu,
            "serverchan": ch_serverchan, "pushplus": ch_pushplus,
            "telegram": ch_telegram, "ntfy": ch_ntfy, "webhook": ch_webhook,
            "email": ch_email}


# ───────────────────────────── 主流程 ─────────────────────────────
def notify(cfg, title, text, result=None, attach=(), reason="", quiet=False):
    """按配置顺序尝试渠道，第一个成功就停。全部失败返回 False。"""
    chans = configured_channels(cfg)
    if not chans:
        if not quiet:
            print("没有配置任何推送渠道。")
            print("提示：跑 `wdradar` 选 n 设定推送方式；最省事的是企业微信机器人。")
        return False
    for c in chans:
        try:
            how = CHANNELS[c](cfg, title, text, result, attach, reason=reason)
            print("✓ 已推送：%s" % how)
            return True
        except Exception as e:                                       # noqa: BLE001
            print("✗ %s 失败：%s" % (CHANNEL_HELP.get(c, c), str(e)[:200]))
    print("所有渠道都失败了 —— 报告已落盘，用 `wdradar report` 看路径。")
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default="")
    ap.add_argument("--test", action="store_true")
    ap.add_argument("--failure", action="store_true")
    ap.add_argument("--reason", default="west.cn 限流或抓取结果为空（详见日志）")
    ap.add_argument("--list", action="store_true")
    args = ap.parse_args()

    cfg = load_config()

    if args.list:
        chans = configured_channels(cfg)
        print("可用渠道（按尝试顺序）：%s" % ("、".join(chans) or "（无）"))
        print()
        for c in CHANNEL_ORDER:
            if c in chans:
                print("  ✓ %-11s %s" % (c, CHANNEL_HELP[c]))
            else:
                miss = missing_params(cfg, c)
                if miss:
                    print("  · %-11s %s   （缺：%s）" % (c, CHANNEL_HELP[c], "、".join(miss)))
                else:
                    print("  · %-11s %s" % (c, CHANNEL_HELP[c]))
        if not chans:
            print()
            print("一个都没配好。跑 `wdradar` 选 n 设定推送方式，最省事的是企业微信机器人。")
        return

    date_str = args.date or dt.date.today().isoformat()

    if args.test:
        demo = {"date": date_str, "top10": [
            {"domain": "demo.com", "cls_label": "可发音英文", "score": 88,
             "regdate": 2010, "reasons": ["可发音英文", "域龄 16 年"]},
            {"domain": "demo.cn", "cls_label": "英文单词", "score": 100,
             "regdate": 2005, "premium": True, "premiumprice": 588,
             "reasons": ["英文单词"]}],
            "by_class": {"en_pron": 1, "en_word": 1}, "watch": [], "all_items": []}
        text = "这是一条**测试推送**，说明渠道配好了。\n\n" + build_text(demo, date_str)
        sys.exit(0 if notify(cfg, "[测试] " + build_title(date_str), text, demo) else 1)

    if args.failure:
        if (cfg.get("NOTIFY_ON_FAILURE", "1") or "1") == "0":
            print("NOTIFY_ON_FAILURE=0，不发失败告知")
            return
        notify(cfg, build_title(date_str, failure=True),
               build_text(None, date_str, failure=args.reason), None, reason=args.reason)
        return

    jpath = os.path.join(REPORTS, "%s.json" % date_str)
    if not os.path.exists(jpath):
        print("找不到 %s" % jpath, file=sys.stderr)
        sys.exit(2)
    result = json.load(open(jpath, encoding="utf-8"))
    attach = [os.path.join(REPORTS, "%s.html" % date_str),
              os.path.join(REPORTS, "%s.md" % date_str)]
    sys.exit(0 if notify(cfg, build_title(date_str), build_text(result, date_str),
                         result, attach) else 1)


if __name__ == "__main__":
    main()
