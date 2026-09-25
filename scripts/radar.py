#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
西部数码(w est.cn) 过期域名雷达 v2
====================================
数据源：https://www.west.cn/booking/  ->  /services/grabnew/newlist.asp

核心变化（v2）
  1) 一次查询三个后缀：`arrdomext=com,cn,top`，请求量从 28 降到 ~16，更快也更不容易被限流。
  2) 选品逻辑按用户偏好分四层（见 scripts/lang.py）：
       英文单词 > 可发音英文 > 双拼/三拼 > 拼音首字母（必须对应常用中文四字词）
     对应不上任何一层的（如 rpkc.cn、jxggw.com）直接淘汰，不进榜。
  3) 报告按"语言类别"分组，而不是按后缀。

⚠️ 站点限制（v1 踩过的坑，务必保留）
  - pagesize 硬上限 50；第 2 页起返回 code=6666 需登录；请求过快 → code=500 封 IP（持续很久，
    4 个并发即可触发）。所以：严格串行、间隔 ≥2.5s、单次 ≤30 次请求。
  - `domkey` 每次最多 20 个关键词。接口也支持 GET。

用法：
  python radar.py                      # 抓取并出报告
  python radar.py --json-only          # 报告照常落盘，stdout 输出 JSON
  python radar.py --dates com=..,cn=..,top=..   # 手动指定删除日期（省 9 次探测请求）
  python radar.py --today 2026-09-24   # 指定基准日（调试/补跑）
"""

import argparse
import datetime as dt
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import lang                                                          # noqa: E402

SKILL_DIR = os.path.dirname(HERE)
ASSETS = os.path.join(SKILL_DIR, "assets")
REPORTS = os.path.join(SKILL_DIR, "reports")
STATE_FILE = os.path.join(SKILL_DIR, "state.json")

API = "https://www.west.cn/services/grabnew/newlist.asp"
PAGE_SIZE = 50
MIN_INTERVAL = 2.5              # 串行节流（秒），别低于 1.5
MAX_RETRY_ON_BUSY = 3
BUSY_WAIT = 25

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"),
    "Referer": "https://www.west.cn/booking/",
    "X-Requested-With": "XMLHttpRequest",
    "Content-Type": "application/x-www-form-urlencoded",
}

# ---------------------------------------------------------------- 口径
EXT_ALL = "com,cn,top"
OTHERCN = "n"                   # 只要一级 cn
DIGITS_AND_HYPHEN = "0,1,2,3,4,5,6,7,8,9,-"
SUFFIX_RULE = {                 # 各后缀允许的主体长度
    "com": (5, 8),
    "cn": (2, 8),
    "top": (2, 8),
}

# 每个"视图"= 1 次请求（已含三个后缀）。
# 注意：接口的 deldate 只能传单值，而三个后缀的目标删除日期各不相同，
# 所以抓取时是"对每个目标日期各跑一遍这些视图"，而不是一次覆盖所有日期。
VIEWS = [
    ("最短档(4~5位)", {"domlen1": 4, "domlen2": 8, "ordby": "domlen", "ordtp": "asc"}),
    ("6位档", {"domlen1": 6, "domlen2": 8, "ordby": "domlen", "ordtp": "asc"}),
    ("估价最高", {"ordby": "refsmoney", "ordtp": "desc"}),
]
# ≤4 位的小池子（几百条以内）做**完整枚举**，不抽样 —— 4 位正是"可发音英文/双拼/声母"的密集区，
# 抽样 50 条会漏掉用户看中的米（实测 sery.cn / glax.cn 就是这么漏掉的）。
ENUM_MAXLEN = 4

ALPHABET = "abcdefghijklmnopqrstuvwxyz"
# 单次运行的接口请求硬上限。**任何新增抓取逻辑都必须尊重这个上限**——
# 2026-09-24 因为枚举逻辑无界递归，单次打出 120+ 请求，把配额烧光、IP 再次被封。
REQUEST_CAP = 30
LOOKAHEAD_DAYS = 5
VERIFY_RATIO = 0.10
VERIFY_MIN = 30
FALLBACK_OFFSET = {"com": 4, "cn": 1, "top": 5}

# ---------------------------------------------------------------- 打分修饰
LEN_BONUS = {2: 8, 3: 7, 4: 6, 5: 5, 6: 4, 7: 3, 8: 2}
SUFFIX_BONUS = {"com": 6, "cn": 3, "top": 1}
MOD_CAP = 28                    # 修饰分上限，保证"类别优先级"仍是主序


class BusyError(RuntimeError):
    pass


_last_call = [0.0]
_stats = {"requests": 0, "busy": 0}


# ---------------------------------------------------------------- HTTP
def api_post(params, retries=MAX_RETRY_ON_BUSY):
    p = {k: v for k, v in params.items() if v is not None}
    body = urllib.parse.urlencode(p).encode()
    for attempt in range(retries + 1):
        gap = MIN_INTERVAL - (time.time() - _last_call[0])
        if gap > 0:
            time.sleep(gap)
        _last_call[0] = time.time()
        _stats["requests"] += 1
        try:
            req = urllib.request.Request(API, data=body, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=45) as r:
                raw = r.read()
            try:
                txt = raw.decode("utf-8")
            except UnicodeDecodeError:
                txt = raw.decode("gb18030", "replace")       # 站点中文是 GBK
            j = json.loads(txt)
        except Exception as e:                                       # noqa: BLE001
            if attempt >= retries:
                raise RuntimeError("接口请求失败: %s" % e)
            time.sleep(3 * (attempt + 1))
            continue
        # 该接口的 500 只有"查询太频繁"这一种含义（msg 是 GBK，不做文字匹配，避免漏判）
        if j.get("code") == 500:
            _stats["busy"] += 1
            if attempt >= retries:
                raise BusyError("被 west.cn 限流（code=500 %s），已重试 %d 次"
                                % ((j.get("msg") or "").strip(), retries))
            time.sleep(BUSY_WAIT * (attempt + 1))
            continue
        return j
    raise RuntimeError("unreachable")


def api_total(**kw):
    j = api_post(dict(kw, pageno=1, pagesize=1))
    return int((j.get("body") or {}).get("total") or 0) if j.get("code") == 200 else 0


# ---------------------------------------------------------------- 删除日期
def _load_state():
    if os.path.exists(STATE_FILE):
        try:
            return json.load(open(STATE_FILE, encoding="utf-8"))
        except Exception:                                            # noqa: BLE001
            pass
    return {}


def _save_state(s):
    try:
        json.dump(s, open(STATE_FILE, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    except Exception:                                                # noqa: BLE001
        pass


def resolve_deldate(today, override=None):
    """阈值 = 当天库存 ×10%（下限30）；取窗口内仍"开仓"的最靠后日期。
    从缓存偏移出发验证 → 能前推就前推、否则回退，一般每个后缀 2~3 次请求。"""
    state, out = _load_state(), {}
    for ext in ("com", "cn", "top"):
        if override and ext in override:
            out[ext] = (override[ext], {"mode": "manual", "counts": {},
                                        "offset": (dt.date.fromisoformat(override[ext]) - today).days})
            continue
        counts = {}

        def cnt(off):
            d = (today + dt.timedelta(days=off)).isoformat()
            if d not in counts:
                counts[d] = api_total(arrdomext=ext, deldate=d)
            return counts[d]

        base = cnt(0)
        thr = max(base * VERIFY_RATIO, VERIFY_MIN)
        cached = state.get(ext, {}).get("offset")
        off = cached if isinstance(cached, int) else FALLBACK_OFFSET[ext]
        off = max(0, min(off, LOOKAHEAD_DAYS))
        mode = "offset(cached)" if isinstance(cached, int) else "offset(default)"
        chosen = None
        if cnt(off) >= thr:
            while off < LOOKAHEAD_DAYS and cnt(off + 1) >= thr:
                off += 1
            chosen = off
        else:
            for o in range(off - 1, -1, -1):
                if cnt(o) >= thr:
                    chosen, mode = o, "offset(backward)"
                    break
        if chosen is None:
            mode = "probe"
            for i in range(LOOKAHEAD_DAYS + 1):
                cnt(i)
            peak = max(counts.values()) if counts else 0
            t2 = max(peak * VERIFY_RATIO, VERIFY_MIN)
            for i in range(LOOKAHEAD_DAYS, -1, -1):
                if counts.get((today + dt.timedelta(days=i)).isoformat(), 0) >= t2:
                    chosen = i
                    break
        if chosen is None:
            chosen, mode = FALLBACK_OFFSET[ext], "fallback"
        state.setdefault(ext, {}).update({"offset": chosen, "verified_on": today.isoformat()})
        out[ext] = ((today + dt.timedelta(days=chosen)).isoformat(),
                    {"mode": mode, "counts": counts, "offset": chosen,
                     "threshold": round(thr), "today_total": base})
    _save_state(state)
    return out


# ---------------------------------------------------------------- 抓取
def _collect(items, got):
    n = 0
    for it in got:
        d = (it.get("domain") or "").lower()
        if d and d not in items:
            items[d] = it
            n += 1
    return n


def _enum_group(base, prefixes, items, log, allow_split=True):
    """把一组首字母打包成一次查询。超过一页时**最多二分一次**，再超就直接取第一页并记警告。

    ⚠️ 教训：这里绝对不能再做"无限递归拆分"。之前写成递归二分（单字母还超就拆两字母、
    两字母还超再拆三字母……）结果单次运行打出 120+ 个请求，5 分钟就把配额烧光、IP 再次被封。
    现在全程请求数有界：分组数 ≤ 10，每组最多 1 次 + 1 次二分。
    """
    j = api_post(dict(base, domkey=",".join(prefixes), domeq="1"))
    b = j.get("body") or {}
    total = int(b.get("total") or 0)
    if total <= PAGE_SIZE:
        _collect(items, b.get("items") or [])
        return
    if allow_split and len(prefixes) > 1:
        mid = len(prefixes) // 2
        _enum_group(base, prefixes[:mid], items, log, False)
        _enum_group(base, prefixes[mid:], items, log, False)
        return
    _collect(items, b.get("items") or [])
    log("  ⚠ 分区 %s* 有 %d 条超出一页，只取到前 %d 条" % (prefixes[0], total, PAGE_SIZE))


def fetch_small_pools(dates, items, log=lambda s: None):
    """把 ≤4 位的小池子尽量**取全**（这部分不能靠抽样）。

    为什么要取全：4 位正是"可发音英文 / 双拼 / 声母"的密集区，抽样 50 条会漏掉用户看中的米
    （实测 sery.cn、glax.cn 就因此漏掉，用户直接质问）。

    怎么绕开"第 2 页要登录"：用 `domkey` 按首字母**打包分区**（一次可传 20 个关键词）。
    只枚举**最早那个目标日期**（用户最先要动手的那批），分组数按池内总量估算、硬上限 10 组，
    所以这一段的请求数固定在 1+10 次以内。
    """
    earliest = min(d[0] for d in dates.values())
    base = {"arrdomext": EXT_ALL, "othercn": OTHERCN, "deldate": earliest,
            "domlen1": 1, "domlen2": ENUM_MAXLEN, "domunkey": DIGITS_AND_HYPHEN,
            "pageno": 1, "pagesize": PAGE_SIZE}
    j = api_post(dict(base))
    b = j.get("body") or {}
    total = int(b.get("total") or 0)
    if not total:
        return
    if total <= PAGE_SIZE:
        n = _collect(items, b.get("items") or [])
        log("  ◆ %s ≤%d位：池内 %d 条，一次取全（新增 %d）" % (earliest, ENUM_MAXLEN, total, n))
        return
    groups = max(1, min(7, -(-total // 45)))             # 每组≈45 条，最多 7 组（控请求数）
    step = -(-len(ALPHABET) // groups)
    pkgs = [ALPHABET[i:i + step] for i in range(0, len(ALPHABET), step)]
    before = len(items)
    for pkg in pkgs:
        if _stats["requests"] >= REQUEST_CAP:
            log("  ⛔ 已达请求上限 %d，≤4位枚举提前结束" % REQUEST_CAP)
            break
        _enum_group(base, list(pkg), items, log)
    log("  ◆ %s ≤%d位：池内 %d 条，分区枚举 %d 组（新增 %d）"
        % (earliest, ENUM_MAXLEN, total, len(pkgs), len(items) - before))


def fetch_watch(domains, log=lambda s: None):
    """定向核查：把用户关心的域名拿去问接口（`deldate=wei` = 所有待删除可预订）。
    每次请求最多 20 个关键词（站点的硬限制）。"""
    out = {}
    if not domains:
        return out
    for i in range(0, len(domains), 20):
        batch = domains[i:i + 20]
        try:
            j = api_post({"arrdomext": EXT_ALL, "othercn": OTHERCN, "deldate": "wei",
                          "domkey": ",".join(batch), "pageno": 1, "pagesize": PAGE_SIZE})
        except BusyError:
            raise
        except Exception as e:                                        # noqa: BLE001
            log("  ⚠ 定向核查失败：%s" % e)
            continue
        for it in (j.get("body") or {}).get("items") or []:
            d = (it.get("domain") or "").lower()
            if d:
                out[d] = it
    log("  定向核查 %d 个域名 → 命中 %d 个" % (len(domains), len(out)))
    return out


def fetch_all(dates, items, log=lambda s: None):
    """对每个目标删除日期各跑一遍 VIEWS（每次请求覆盖 com+cn+top 三个后缀）。
    结果并入传入的 items 字典。返回 {日期: 池内总量}。"""
    totals = {}
    for ext, (deldate, _meta) in dates.items():
        for vname, vp in VIEWS:
            if _stats["requests"] >= REQUEST_CAP:
                log("  ⛔ 已达请求上限 %d，停止继续抓取（本轮报告基于已取到的部分）" % REQUEST_CAP)
                return totals
            params = {
                "arrdomext": EXT_ALL, "othercn": OTHERCN, "deldate": deldate,
                "domlen1": 2, "domlen2": 8, "domunkey": DIGITS_AND_HYPHEN,
                "pageno": 1, "pagesize": PAGE_SIZE,
            }
            params.update(vp)                                   # 视图参数覆盖默认长度
            j = api_post(params)
            b = j.get("body") or {}
            if vname == VIEWS[0][0]:                             # 首视图 → 该日期池内总量
                totals[deldate] = int(b.get("total") or 0)
            got = b.get("items") or []
            log("  · %s / %-12s 取回 %d 条" % (deldate, vname, len(got)))
            _collect(items, got)
    return totals


# ---------------------------------------------------------------- 打分
def _int(v, default=0):
    try:
        return int(float(v)) if v not in (None, "") else default
    except (TypeError, ValueError):
        return default


def _tipinfo(html):
    if not html:
        return 0, 0
    y = re.search(r"建站\s*(\d+)\s*年", html)
    b = re.search(r"收录\s*(\d+)\s*条", html)
    return (int(y.group(1)) if y else 0), (int(b.group(1)) if b else 0)


def score_item(it, today):
    """返回 dict（含 cls/score/reasons），或 None 表示不值得关注"""
    dom = (it.get("domain") or "").lower()
    parts = dom.split(".")
    if len(parts) != 2:
        return None
    label, ext = parts
    if ext not in SUFFIX_RULE:
        return None
    lo, hi = SUFFIX_RULE[ext]
    if not (lo <= len(label) <= hi) or not re.fullmatch(r"[a-z]+", label):
        return None

    c = lang.classify(label)
    if not c:                                  # 单词/拼音/可发音/四字词首字母 全不沾 → 淘汰
        return None

    mod, reasons = [], list([c["reason"]])
    age = max(today.year - _int(it.get("regdate")), 0) if _int(it.get("regdate")) else 0
    hist, bd = _tipinfo(it.get("tipinfo"))
    ref = _int(it.get("refsmoney"))
    hot = _int(it.get("isyd")) == 1 or _int(it.get("isyuding")) == 1
    premium = bool(it.get("ispremium"))

    mod.append(LEN_BONUS.get(len(label), 0))
    mod.append(SUFFIX_BONUS.get(ext, 0))
    # "可发音英文"里长度差异要拉开：8 位随机拼接词（conalmat）价值远低于 4~5 位（kombio/sery）
    if c["cls"] == "en_pron":
        mod.append({3: 0, 4: 2, 5: 0, 6: -3, 7: -6, 8: -9}.get(len(label), 0))
    if age >= 20:
        mod.append(10)
        reasons.append("域龄 %d 年" % age)
    elif age >= 15:
        mod.append(8)
        reasons.append("域龄 %d 年" % age)
    elif age >= 10:
        mod.append(6)
        reasons.append("域龄 %d 年" % age)
    elif age >= 5:
        mod.append(4)
    if hist >= 10:
        mod.append(8)
        reasons.append("建站 %d 年" % hist)
    elif hist >= 5:
        mod.append(5)
        reasons.append("建站 %d 年" % hist)
    elif hist >= 1:
        mod.append(3)
    if bd >= 1000:
        mod.append(5)
        reasons.append("百度收录 %d" % bd)
    elif bd >= 100:
        mod.append(3)
        reasons.append("百度收录 %d" % bd)
    if ref >= 1000:
        mod.append(6)
        reasons.append("估价 %d 元" % ref)
    elif ref >= 100:
        mod.append(2)
        reasons.append("估价 %d 元" % ref)
    if hot:
        mod.append(3)
        reasons.append("⚠️ 已有人预订，会进竞拍")
    if premium:
        mod.append(2)
        reasons.append("⚠️ 溢价域名，预订需额外费用")
    risk = 0
    if str(it.get("wallcheck")) == "3":
        risk -= 8
    if str(it.get("wxcheck")) == "2":
        risk -= 8
    if str(it.get("qqcheck")) == "2":
        risk -= 4
    if str(it.get("ismiiban")) == "1":
        risk -= 10
    if str(it.get("ishui")) == "1":
        risk -= 6
    if risk:
        reasons.append("⚠️ 存在风险标记")
    mod.append(risk)

    mods = max(-40, min(MOD_CAP, sum(mod)))
    score = round(c["score_hint"] + mods)
    return {"domain": dom, "label": label, "len": len(label), "ext": ext,
            "cls": c["cls"], "cls_label": lang.CLASS_LABEL[c["cls"]],
            "score": score, "base": round(c["score_hint"]), "mods": mods,
            "detail": c["detail"], "reasons": reasons,
            "regdate": it.get("regdate"), "deldate": it.get("deldate"),
            "refsmoney": ref, "hot": hot, "premium": premium,
            "premiumprice": _int(it.get("premiumprice")),
            "tipinfo": it.get("tipinfo") or "", "raw": it}


def build_top(result):
    def key(x):
        return (-x["score"], lang.CLASS_ORDER.index(x["cls"]), x["len"], x["domain"])
    pool = sorted(result["all_items"], key=key)
    return pool


# ---------------------------------------------------------------- 报告
def build_report(result, today):
    L = []
    A = L.append
    A("# 过期域名雷达 · %s" % today.isoformat())
    A("")
    A("> 数据源：" + (result.get("data_source")
                     or "west.cn 过期域名抢注列表（一次查询 com+cn+top）"))
    A("> 选品优先级：**英文单词 > 可发音英文 > 双拼/三拼 > 拼音首字母**；"
      "不沾边的（随机字母）直接淘汰")
    A("")
    A("## 一、本轮信息")
    A("")
    A("| 后缀 | 删除日期 | 距今天数 | 判定方式 | 当日库存 |")
    A("| --- | --- | --- | --- | --- |")
    for ext in ("com", "cn", "top"):
        if ext not in result["dates"]:
            continue
        d, meta = result["dates"][ext]
        off = meta.get("offset")
        A("| .%s | %s | %s | %s | %s |" % (
            ext, d, ("+%d" % off) if isinstance(off, int) else "—",
            MODE_LABEL.get(meta.get("mode"), meta.get("mode")),
            meta.get("counts", {}).get(d) or meta.get("today_total") or "—"))
    A("")
    if result.get("pool_totals"):                       # 22.cn 源常拿不到池内总量，别显示误导性的 0
        A("采样日期池内总量：%s（合计 %s%s）"
          % ("、".join("%s→%s" % (k[5:] if len(k) == 10 and k[4] == "-" else k, v)
                       for k, v in sorted(result["pool_totals"].items())),
             result.get("pool_total", "—"),
             "，为各类别池求和、类别间有重叠" if "22.cn" in (result.get("data_source") or "") else ""))
        A("")
    A("本轮取样 **%d** 条（多后缀合并），其中**值得关注的 %d** 条。"
      % (result.get("sampled", 0), len(result["all_items"])))
    A("")
    A("## 二、最值得关注的 TOP 10")
    A("")
    A("| # | 域名 | 类别 | 评分 | 原注册 | 关注理由 |")
    A("| --- | --- | --- | --- | --- | --- |")
    for i, it in enumerate(result["top10"], 1):
        A("| %d | **%s** | %s | %d | %s | %s |" % (
            i, it["domain"], it["cls_label"], it["score"], it["regdate"] or "—",
            "、".join(it["reasons"])))
    A("")
    A("## 三、按类别看")
    for ck in lang.CLASS_ORDER:
        pool = [x for x in result["all_items"] if x["cls"] == ck]
        A("")
        A("### %s（%d 个）" % (lang.CLASS_LABEL[ck], len(pool)))
        A("")
        if not pool:
            A("*本轮没有取到。*")
            continue
        for i, it in enumerate(pool[:15], 1):
            extra = ""
            if ck == "pinyin_abbr":
                extra = "　可对应：" + "、".join(w for _, w in it["detail"]["matches"])
            elif ck == "pinyin" and it["detail"].get("word"):
                extra = "　（%s）" % it["detail"]["word"]
            A("%d. **%s** — %d 分，原注册 %s%s" % (
                i, it["domain"], it["score"], it["regdate"] or "—", extra))
    A("")
    warn = [x for x in result["all_items"] if x["hot"] or x["premium"]]
    A("## 四、费用提示（会进竞拍 / 需额外付费）")
    A("")
    if warn:
        for it in warn[:12]:
            tags = []
            if it["hot"]:
                tags.append("已有人预订→竞拍")
            if it["premium"]:
                tags.append("溢价域名（%s 元）" % (it["premiumprice"] or "?"))
            A("- **%s** — %s" % (it["domain"], "、".join(tags)))
    else:
        A("*本轮取样里没有被预订或溢价的域名。*")
    A("")
    watch = result.get("watch") or []
    if watch:
        A("## 五、定向核查（你点名的域名是否还在）")
        A("")
        A("| 域名 | 长度 | 删除日期 | 原注册 | 状态 |")
        A("| --- | --- | --- | --- | --- |")
        for w in watch:
            if w.get("included") is False:
                A("| **%s** | %d | — | — | ⚠️ 不在本轮采集的分类池中（未验证） |"
                  % (w["domain"], w["len"]))
                continue
            st = []
            st.append("⚠️ 已被预订" if w["isyuding"] else "✅ 仍在待删除池中")
            if w["premium"]:
                st.append("溢价 %s 元" % (w["premiumprice"] or "?"))
            A("| **%s** | %d | %s | %s | %s |" % (
                w["domain"], w["len"], w["deldate"] or "—", w["regdate"] or "—", "、".join(st)))
        missing = [d for d in (result.get("watch_missing") or [])]
        if missing:
            A("")
            A("未在待删除池中查到：%s" % "、".join(missing))
        A("")
    A("---")
    A("")
    A(result.get("sample_note") or (
        "抽样说明：站点限制匿名单次查询 50 条、不可翻页、请求过快会封 IP，"
        "所以按「估价最高 / 注册最早 / 默认 / 4~7 位长度分档」7 个视图各取一页，"
        "三后缀合并后归类打分。不是全量枚举。"))
    A("")
    A("本次共请求接口 %d 次（限流重试 %d 次）。生成时间：%s"
      % (result["requests"], result["busy"], result["generated_at"]))
    return "\n".join(L)


MODE_LABEL = {"manual": "手动指定", "probe": "窗口探测", "fallback": "兜底偏移",
              "offline": "离线样本", "refilter": "口径重算", "cache": "缓存复用",
              "source22": "22.cn 源"}
CLASS_CSS = {"en_word": "c1", "en_pron": "c2", "pinyin": "c3", "pinyin_abbr": "c4"}


def build_html(result, today):
    def esc(s):
        return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    rows = []
    for i, it in enumerate(result["top10"], 1):
        cls = "gold" if i == 1 else "silver" if i == 2 else "bronze" if i == 3 else ""
        rows.append('<tr class="%s"><td class="rk">%d</td><td class="dm">%s</td>'
                    '<td><span class="tag %s">%s</span></td><td class="sc">%d</td>'
                    '<td>%s</td><td class="rs">%s</td></tr>' % (
                        cls, i, esc(it["domain"]), CLASS_CSS[it["cls"]], esc(it["cls_label"]),
                        it["score"], it["regdate"] or "—", esc("、".join(it["reasons"]))))

    drs = []
    for ext in ("com", "cn", "top"):
        if ext not in result["dates"]:
            continue
        d, meta = result["dates"][ext]
        off = meta.get("offset")
        drs.append("<tr><td class='dm'>.%s</td><td class='dm'>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>"
                   % (ext, d, ("+%d" % off) if isinstance(off, int) else "—",
                      MODE_LABEL.get(meta.get("mode"), meta.get("mode")),
                      meta.get("counts", {}).get(d) or meta.get("today_total") or "—"))

    cats = []
    for ck in lang.CLASS_ORDER:
        pool = [x for x in result["all_items"] if x["cls"] == ck]
        items_html = []
        for it in pool[:15]:
            extra = ""
            if ck == "pinyin_abbr":
                extra = "　可对应：" + "、".join(w for _, w in it["detail"]["matches"])
            elif ck == "pinyin" and it["detail"].get("word"):
                extra = "　（%s）" % it["detail"]["word"]
            items_html.append("<li><b>%s</b><span>%d 分 · 原注册 %s%s</span></li>" % (
                esc(it["domain"]), it["score"], it["regdate"] or "—", esc(extra)))
        cats.append("<div class='cat'><h3><span class='tag %s'>%s</span> <em>%d 个</em></h3><ul>%s</ul></div>"
                    % (CLASS_CSS[ck], esc(lang.CLASS_LABEL[ck]), len(pool),
                       "".join(items_html) or "<li class='empty'>本轮没有取到</li>"))

    warn = [x for x in result["all_items"] if x["hot"] or x["premium"]]
    warn_html = "".join("<li><b>%s</b><span>%s</span></li>" % (
        esc(it["domain"]),
        esc("、".join((["已有人预订→竞拍"] if it["hot"] else []) +
                      (["溢价域名（%s 元）" % (it["premiumprice"] or "?")] if it["premium"] else []))))
        for it in warn[:12]) or "<li class='empty'>本轮取样里没有被预订或溢价的域名</li>"

    watch = result.get("watch") or []
    watch_html = ""
    if watch:
        wr = []
        for w in watch:
            if w.get("included") is False:
                wr.append("<tr><td class='dm'>%s</td><td>%d</td><td>—</td><td>—</td>"
                          "<td>⚠️ 不在本轮采集的分类池中（未验证）</td></tr>" % (esc(w["domain"]), w["len"]))
                continue
            st = ["⚠️ 已被预订" if w["isyuding"] else "✅ 仍在待删除池中"]
            if w["premium"]:
                st.append("溢价 %s 元" % (w["premiumprice"] or "?"))
            wr.append("<tr><td class='dm'>%s</td><td>%d</td><td>%s</td><td>%s</td><td>%s</td></tr>"
                      % (esc(w["domain"]), w["len"], w["deldate"] or "—",
                         w["regdate"] or "—", esc("、".join(st))))
        watch_html = ("<h2>五、定向核查（点名的域名是否还在）</h2>"
                      "<table><tr><th>域名</th><th>长度</th><th>删除日期</th>"
                      "<th>原注册</th><th>状态</th></tr>%s</table>" % "".join(wr))

    return """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>过期域名雷达 %s</title>
<style>
:root{--bg:#0f1115;--card:#181b22;--line:#272b34;--fg:#e8eaee;--mut:#9aa3b2;--acc:#ffb300;--hot:#e8433c}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);
font:14px/1.65 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif;padding:26px 30px}
h1{font-size:22px;margin:0 0 6px}h2{font-size:15px;margin:26px 0 10px;color:var(--acc)}
h3{font-size:14px;margin:0 0 8px}h3 em{font-style:normal;color:var(--mut);font-size:12px;font-weight:400}
.sub{color:var(--mut);font-size:12px;margin-bottom:16px;line-height:1.7}
table{width:100%%;border-collapse:collapse;background:var(--card);border-radius:10px;overflow:hidden}
th,td{padding:9px 12px;text-align:left;border-bottom:1px solid var(--line);font-size:13px}
th{background:#1e222b;color:var(--mut);font-weight:600;font-size:12px}
td.rk{color:var(--mut);width:40px;text-align:center}
td.dm{font-weight:700;color:#fff;font-family:Consolas,monospace}
td.sc{color:var(--hot);font-weight:700}
td.rs{color:var(--mut);font-size:12px}
tr.gold td.dm{color:#ffd24a}tr.silver td.dm{color:#d8dee9}tr.bronze td.dm{color:#e0a06a}
tr:last-child td{border-bottom:none}
.tag{padding:1px 8px;border-radius:99px;font-size:11px;white-space:nowrap}
.tag.c1{background:#2b3a25;color:#a8e06a}.tag.c2{background:#243040;color:#7fc4ff}
.tag.c3{background:#3a2f45;color:#d9a6ff}.tag.c4{background:#3d3524;color:#ffd97a}
.cat{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px;margin-bottom:12px}
.cat ul{margin:0;padding-left:18px}.cat li{margin:5px 0}
.cat li.empty{color:var(--mut);list-style:none;margin-left:-18px}
.cat li span{color:var(--mut);font-size:12px;margin-left:8px}
.note{color:var(--mut);font-size:12px;margin-top:20px;line-height:1.8}
</style></head><body>
<h1>过期域名雷达 · %s</h1>
<div class="sub">数据源：%s<br>
选品优先级：<b>英文单词 &gt; 可发音英文 &gt; 双拼/三拼 &gt; 拼音首字母</b>；不沾边的随机字母直接淘汰</div>
<h2>一、本轮信息</h2>
<table><tr><th>后缀</th><th>删除日期</th><th>距今天数</th><th>判定方式</th><th>当日库存</th></tr>%s</table>
<div class="sub">%s本轮取样 <b>%s</b> 条（多后缀合并）｜ 其中值得关注 <b>%s</b> 条</div>
<h2>二、最值得关注的 TOP 10</h2>
<table><tr><th>#</th><th>域名</th><th>类别</th><th>评分</th><th>原注册</th><th>关注理由</th></tr>%s</table>
<h2>三、按类别看</h2>%s
<h2>四、费用提示（会进竞拍 / 需额外付费）</h2>
<div class="cat"><ul>%s</ul></div>
%s
<div class="note">%s<br>本次共请求接口 %s 次（限流重试 %s 次）。生成时间 %s。</div>
</body></html>""" % (today.isoformat(), today.isoformat(),
                     esc(result.get("data_source") or "west.cn 过期域名抢注列表（一次查询 com+cn+top）"),
                     "".join(drs),
                     (esc("采样日期池内总量：%s（合计 %s%s）<br>" % (
                         "、".join("%s→%s" % (k[5:] if len(k) == 10 and k[4] == "-" else k, v)
                                   for k, v in sorted(result["pool_totals"].items())),
                         result.get("pool_total", "—"),
                         "，为各类别池求和、类别间有重叠"
                         if "22.cn" in (result.get("data_source") or "") else ""))
                      if result.get("pool_totals") else ""),
                     result.get("sampled", 0),
                     len(result["all_items"]), "".join(rows), "".join(cats),
                     warn_html, watch_html, esc(result.get("sample_note") or ""),
                     result["requests"], result["busy"], result["generated_at"])


# ---------------------------------------------------------------- 主流程
def parse_dates_arg(s):
    out = {}
    for part in s.split(","):
        if "=" in part:
            k, v = part.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json-only", action="store_true")
    ap.add_argument("--dates", default="")
    ap.add_argument("--today", default="")
    ap.add_argument("--outdir", default=REPORTS)
    ap.add_argument("--watch", default="", help="定向核查的域名清单文件（每行一个）；"
                                                "默认自动读 assets/watchlist.txt")
    args = ap.parse_args()

    today = dt.date.fromisoformat(args.today) if args.today else dt.date.today()
    os.makedirs(args.outdir, exist_ok=True)
    log = lambda s: print(s, file=sys.stderr)                        # noqa: E731

    dates = resolve_deldate(today, parse_dates_arg(args.dates))
    log("删除日期：" + "、".join("%s=%s" % (k, v[0]) for k, v in dates.items()))

    pool, watched = {}, {}
    try:
        pool_totals = fetch_all(dates, pool, log)
        fetch_small_pools(dates, pool, log)
        wf = args.watch or os.path.join(ASSETS, "watchlist.txt")
        if os.path.exists(wf):
            doms = [l.strip().lower() for l in open(wf, encoding="utf-8", errors="ignore")
                    if l.strip() and not l.startswith("#")]
            watched = fetch_watch(doms, log)
    except BusyError as e:
        print("ERROR: %s（已中断，避免加重封禁）" % e, file=sys.stderr)
        sys.exit(3)

    items = list(pool.values())
    scored = []
    for it in items:
        r = score_item(it, today)
        if r:
            scored.append(r)
    scored.sort(key=lambda x: (-x["score"], lang.CLASS_ORDER.index(x["cls"]), x["len"], x["domain"]))
    log("取样 %d 条 → 值得关注 %d 条" % (len(items), len(scored)))

    # 兜底：一条都没取到就说明抓取其实失败了（限流/接口变更），绝不能拿"0 结果"覆盖当天报告
    if not items:
        print("ERROR: 抓取结果为空（疑似仍被限流或接口变更），已放弃写报告，避免产生假日报。",
              file=sys.stderr)
        sys.exit(3)

    watch_rows = []
    for d, it in sorted(watched.items()):
        watch_rows.append({
            "domain": d, "deldate": it.get("deldate"), "regdate": it.get("regdate"),
            "len": len(d.split(".")[0]),
            "isyuding": _int(it.get("isyd")) == 1 or _int(it.get("isyuding")) == 1,
            "premium": bool(it.get("ispremium")), "premiumprice": _int(it.get("premiumprice")),
        })

    by_cls = {k: [x for x in scored if x["cls"] == k] for k in lang.CLASS_ORDER}
    result = {
        "date": today.isoformat(), "dates": dates,
        "pool_totals": pool_totals, "pool_total": sum(pool_totals.values()),
        "sampled": len(items), "all_items": scored, "top10": scored[:10],
        "watch": watch_rows,
        "by_class": {k: len(v) for k, v in by_cls.items()},
        "requests": _stats["requests"], "busy": _stats["busy"],
        "generated_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    md, html = build_report(result, today), build_html(result, today)
    stamp = today.isoformat()
    for name, content in (("%s.md" % stamp, md), ("%s.html" % stamp, html)):
        with open(os.path.join(args.outdir, name), "w", encoding="utf-8") as f:
            f.write(content)
    with open(os.path.join(args.outdir, "%s.json" % stamp), "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    if args.json_only:
        json.dump(result, sys.stdout, ensure_ascii=False, indent=2)
    else:
        print(md)


if __name__ == "__main__":
    main()
