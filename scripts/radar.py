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
MIN_INTERVAL = 3.0              # 串行节流（秒），别低于 1.5；配 REQUEST_CAP=34 → ≈12 请求/分钟
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

# 站点可用的排序维度（每个视图 = 1 次请求，用来在一页 50 条的限制下抓"高信号"样本）
VIEW_SPECS = [
    ("最短",     {"ordby": "domlen",    "ordtp": "asc"}),
    ("最早注册", {"ordby": "regdate",   "ordtp": "asc"}),
    ("估价最高", {"ordby": "refsmoney", "ordtp": "desc"}),
]

# 抓取"范围"= (后缀, 长度下限, 长度上限, 用哪几个视图, 是否完整枚举)
#
# ⚠️ 每个范围**只查它自己那个后缀的目标删除日期**，并且长度过滤交给服务端。
# 这是 2026-09-26 那起事故的修复：
#   以前用 arrdomext=com,cn,top 一次查三个后缀，而 deldate 只能传一个值，
#   于是"为了查 com 的 09-30 批次"发出的请求，同时返回了 .top 的 09-30 批次记录；
#   这些记录被无差别塞进候选池，最后霸占了榜单前 5 名——
#   报告明明写着 top = 10-01，榜单却是 09-30 的 service.top / support.top / mint.top。
#   **换出口可以，混批次不行。**
# 拆成单后缀查询后，请求数不变，但每条返回结果都一定属于本批次，信号利用率高得多。
SCOPES = [
    ("top", 2, 4, [],          True),      # ≤4 位池子很小（今天 81 条），先枚举能保底拿到
    ("cn",  2, 4, [],          True),      # 4 位 / 短位：完整枚举（几百条）
    ("com", 5, 5, [0, 1, 2],   False),     # 5 位纯字母 .com 是最值钱的一档
    ("com", 6, 8, [0, 1],      False),
    ("cn",  5, 8, [0, 2],      False),
    ("top", 5, 5, [0, 1, 2],   False),
    ("top", 6, 8, [0, 1],      False),
]
# ≤4 位的小池子做**完整枚举**，不抽样 —— 4 位正是"可发音英文/双拼/声母"的密集区，
# 抽样 50 条会漏掉用户看中的米（实测 sery.cn / glax.cn 就是这么漏掉的）。
ENUM_MAXLEN = 4

ALPHABET = "abcdefghijklmnopqrstuvwxyz"
# 单次运行的接口请求硬上限。**任何新增抓取逻辑都必须尊重这个上限**——
# 2026-09-24 因为枚举逻辑无界递归，单次打出 120+ 请求，把配额烧光、IP 再次被封。
# 2026-09-26 把这轮实测的安全区间定成：总量约 34 次、间隔 3 秒（≈12 请求/分钟），
# 单次跑完约 2.5 分钟。超过这个速率目录不会被封，但也别离得更近。
REQUEST_CAP = 34
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


def _scope_base(ext, lo, hi, deldate):
    """构造某个"范围"的公共查询参数：单后缀 + 该后缀自己的删除日期 + 服务端长度过滤。"""
    p = {"arrdomext": ext, "deldate": deldate, "domlen1": lo, "domlen2": hi,
         "domunkey": DIGITS_AND_HYPHEN, "pageno": 1, "pagesize": PAGE_SIZE}
    if ext == "cn":
        p["othercn"] = OTHERCN                       # 只要一级 cn
    return p


def fetch_scoped(dates, items, log=lambda s: None):
    """按 (后缀, 长度区间) 逐块抓取，每块只用**该后缀自己的**目标删除日期。

    返回 {范围标签: {"total": 池内总量, "fetched": 本轮取回条数}}。
    每条取回的结果都必然属于本批次，所以不再需要跨批次过滤
    （过滤仍然保留在 main() 里做保险，见那里关于 2026-09-26 事故的注释）。
    """
    totals = {}
    for ext, lo, hi, views, _enum in SCOPES:
        if not views:
            continue
        deldate = dates[ext][0]
        label = "%s %d-%d位" % (ext, lo, hi)
        for vi in views:
            if _stats["requests"] >= REQUEST_CAP:
                log("  ⛔ 已达请求上限 %d，停止继续抓取（本轮报告基于已取到的部分）" % REQUEST_CAP)
                return totals
            vname, vp = VIEW_SPECS[vi]
            params = _scope_base(ext, lo, hi, deldate)
            params.update(vp)
            j = api_post(params)
            b = j.get("body") or {}
            if vi == views[0]:                        # 该范围的首个视图 → 建条目 + 池内总量
                totals[label] = {"total": int(b.get("total") or 0), "fetched": 0}
            got = b.get("items") or []
            slot = totals.setdefault(label, {"total": 0, "fetched": 0})
            slot["fetched"] += len(got)
            log("  · %s（%s） / %-6s 取回 %d 条（池内 %s）"
                % (label, deldate, vname, len(got), slot["total"]))
            _collect(items, got)
    return totals


def fetch_small_pools(dates, items, log=lambda s: None):
    """把 ≤4 位的小池子**按后缀分别取全**（这部分不能靠抽样）。

    为什么要取全：4 位正是"可发音英文 / 双拼 / 声母"的密集区，抽样 50 条会漏掉用户看中的米
    （实测 sery.cn、glax.cn 就因此漏掉，用户直接质问；2026-09-26 又因为把这一段的预算
    让给了大池子采样，把 loho.top 挤掉了 —— 所以现在**这一段排在最前面跑**）。

    怎么绕开"第 2 页要登录"：用 `domkey` 按首字母**打包分区**（一次可传 20 个关键词）。
    ⚠️ 必须**按后缀分别枚举**、各自用各自的目标日期 —— 以前三个后缀混在一起只枚举最早的
    那个日期，结果 .top 的 ≤4 位池（10-01）从来没被枚举过，而 .cn 日期下的 .top 记录
    又混了进来。分组数按池内总量估算、硬上限 7 组，请求数有界。

    返回 {范围标签: {"total": 池内总量, "fetched": 本轮取回条数, "full": 是否取全}}。
    """
    totals = {}
    for ext, lo, hi, _views, do_enum in SCOPES:
        if not do_enum:
            continue
        deldate = dates[ext][0]
        label = "%s %d-%d位" % (ext, lo, hi)
        base = _scope_base(ext, lo, hi, deldate)
        if _stats["requests"] >= REQUEST_CAP:
            log("  ⛔ 已达请求上限 %d，跳过 %s 枚举" % (REQUEST_CAP, label))
            continue
        j = api_post(dict(base))
        b = j.get("body") or {}
        total = int(b.get("total") or 0)
        if not total:
            totals[label] = {"total": 0, "fetched": 0, "full": True}
            continue
        got = b.get("items") or []
        if total <= PAGE_SIZE:
            n = _collect(items, got)
            totals[label] = {"total": total, "fetched": len(got), "full": True}
            log("  ◆ %s（%s）：池内 %d 条，一次取全（新增 %d）" % (label, deldate, total, n))
            continue
        groups = max(1, min(7, -(-total // 45)))             # 每组≈45 条，最多 7 组（控请求数）
        step = -(-len(ALPHABET) // groups)
        pkgs = [ALPHABET[i:i + step] for i in range(0, len(ALPHABET), step)]
        before = len(items)
        got_n, done = len(got), 0
        for pkg in pkgs:
            if _stats["requests"] >= REQUEST_CAP:
                log("  ⛔ 已达请求上限 %d，%s 枚举提前结束" % (REQUEST_CAP, label))
                break
            _enum_group(base, list(pkg), items, log)
            done += 1
        # 分区枚举的"取回条数"拿不到精确值（_enum_group 只返回新增数），用新增数近似并标注
        totals[label] = {"total": total, "fetched": got_n + (len(items) - before),
                         "full": False, "groups": "%d/%d" % (done, len(pkgs))}
        log("  ◆ %s（%s）：池内 %d 条，分区枚举 %d/%d 组（新增 %d）"
            % (label, deldate, total, done, len(pkgs), len(items) - before))
    return totals


def fetch_watch(domains, log=lambda s: None):
    """定向核查：把指定域名拿去问接口（`deldate=wei` = 所有待删除可预订）。
    每次请求最多 20 个关键词（站点的硬限制）。

    ⚠️ 2026-09-26：用户明确说"不需要定向核查"，所以**主流程已不再调用这个函数**，
    报告里也不再有第五节。保留它只因为备用的 22.cn 源（source22.py）还在用。
    顺带一个已验证的事实：`domkey` 是**精确匹配**，查不存在的域名会返回 total=0。
    """
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


def scope_of(v):
    """pool_totals 的值：现在是 {"total","fetched","full"}，旧报告里是 int。
    两种都兼容，免得旧脚本喂进来就崩。fetched 未知时返回 None（不要假装是 0）。"""
    if isinstance(v, dict):
        return v.get("total", 0), v.get("fetched"), bool(v.get("full"))
    try:
        return int(v), None, False
    except (TypeError, ValueError):
        return 0, None, False


# ---------------------------------------------------------------- 报告
def build_report(result, today):
    L = []
    A = L.append
    A("# 过期域名雷达 · %s" % today.isoformat())
    A("")
    A("> 数据源：" + (result.get("data_source")
                     or "west.cn 过期域名抢注列表（按后缀分别查询，各用自己的删除日期）"))
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
    if result.get("pool_totals"):                       # 键是 "com 5-5位" 这种范围标签
        A("各范围覆盖情况（池内总量 → 本轮取回）：")
        A("")
        A("| 范围 | 池内总量 | 本轮取回 | 覆盖率 |")
        A("| --- | --- | --- | --- |")
        for k in sorted(result["pool_totals"]):
            t, f, full = scope_of(result["pool_totals"][k])
            mark = "（完整枚举）" if full else ""
            cov = "—" if not f or not t else "%.0f%%" % (100.0 * f / t)
            A("| %s | %s | %s | %s%s |" % (k, t, "—" if f is None else f, cov, mark))
        A("")
        A("> 大池子（com 5-8 位今天有 1.7 万条）受「单次 50 条 + 第 2 页要登录」限制，"
          "只能按排序维度取样，覆盖率低是**站点限制**、不是漏跑；"
          "≤4 位的小池子会尽量取全。")
        A("")
    A("本轮取样 **%d** 条，其中**值得关注的 %d** 条。%s"
      % (result.get("sampled", 0), len(result["all_items"]),
         ("已剔除 %d 条不属于本批次的记录（保险丝，正常应为 0）。"
          % result["dropped_offbatch"]) if result.get("dropped_offbatch") else ""))
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
    d4 = result.get("abbr4_diag")
    if d4:
        A("")
        A("> **四声母诊断**：本批次 4 位域名 **%d** 个（`.com` 从 5 位起，所以四声母只可能来自 "
          "`.cn/.top` 的 ≤4 位池），其中命中好词表的 **%d** 个（好词表共 %d 个四字词首字母组合）。"
          "%s"
          % (d4["four_total"], d4["four_whitelisted"], d4["whitelist"],
             ("　命中：" + "、".join(d4["hits"])) if d4["hits"] else
             "　→ 这批里确实没有你认可的好词，不是没查"))
    A("")
    # 溢价域名已在候选阶段整段排除（用户明确不考虑注册），这里只提示"已有人预订"的竞争风险
    hot = [x for x in result["all_items"] if x["hot"]]
    A("## 四、竞争提示（已有人预订 → 会进竞拍）")
    A("")
    if hot:
        for it in hot[:12]:
            A("- **%s** — 已有人预订，预订后可能进入竞拍" % it["domain"])
    else:
        A("*本轮取样里没有已被人预订的域名。*")
    A("")
    if result.get("excluded_premium"):
        A("> 另有 **%d** 个溢价域名已从结果中剔除（溢价米不考虑注册）。"
          % result["excluded_premium"])
        A("")
    A("---")
    A("")
    A(result.get("sample_note") or (
        "抽样说明：站点限制匿名单次查询 50 条、不可翻页、请求过快会封 IP，"
        "所以**按后缀分别**沿「最短 / 注册最早 / 估价最高」等排序维度各取一页，"
        "≤4 位的小池子用首字母分区尽量取全。**每条记录都必须是该后缀目标删除日期的批次**，"
        "不是全量枚举。"))
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
    d4 = result.get("abbr4_diag")
    if d4:
        cats.append("<div class='note'>四声母诊断：本批次 4 位域名 <b>%d</b> 个"
                    "（.com 从 5 位起，四声母只可能来自 .cn/.top 的 ≤4 位池），"
                    "命中好词表 <b>%d</b> 个（好词表共 %d 个组合）。%s</div>"
                    % (d4["four_total"], d4["four_whitelisted"], d4["whitelist"],
                       esc(("命中：" + "、".join(d4["hits"])) if d4["hits"]
                           else "→ 这批里确实没有你认可的好词，不是没查")))

    hot = [x for x in result["all_items"] if x["hot"]]
    warn_html = "".join("<li><b>%s</b><span>已有人预订，预订后可能进入竞拍</span></li>" % esc(it["domain"])
                       for it in hot[:12]) or "<li class='empty'>本轮取样里没有已被人预订的域名</li>"
    if result.get("excluded_premium"):
        warn_html += ("<li class='empty'>另有 %d 个溢价域名已从结果中剔除（溢价米不考虑注册）</li>"
                      % result["excluded_premium"])

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
<div class="sub">%s本轮取样 <b>%s</b> 条 ｜ 其中值得关注 <b>%s</b> 条%s</div>
<h2>二、最值得关注的 TOP 10</h2>
<table><tr><th>#</th><th>域名</th><th>类别</th><th>评分</th><th>原注册</th><th>关注理由</th></tr>%s</table>
<h2>三、按类别看</h2>%s
<h2>四、竞争提示（已有人预订 → 会进竞拍）</h2>
<div class="cat"><ul>%s</ul></div>
<div class="note">%s<br>本次共请求接口 %s 次（限流重试 %s 次）。生成时间 %s。</div>
</body></html>""" % (today.isoformat(), today.isoformat(),
                     esc(result.get("data_source") or
                         "west.cn 过期域名抢注列表（按后缀分别查询，各用自己的删除日期）"),
                     "".join(drs),
                     (esc("各范围覆盖：" + "　".join(
                         "%s 池内 %s / 本轮取回 %s%s" % (
                             k, scope_of(v)[0],
                             "—" if scope_of(v)[1] is None else scope_of(v)[1],
                             "（完整枚举）" if scope_of(v)[2] else "")
                         for k, v in sorted(result["pool_totals"].items())) + "<br>")
                      if result.get("pool_totals") else ""),
                     result.get("sampled", 0),
                     len(result["all_items"]),
                     (esc("　已剔除 %d 条不属于本批次的记录（保险丝，正常应为 0）"
                          % result["dropped_offbatch"])
                      if result.get("dropped_offbatch") else ""),
                     "".join(rows), "".join(cats),
                     warn_html, esc(result.get("sample_note") or ""),
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
    args = ap.parse_args()

    today = dt.date.fromisoformat(args.today) if args.today else dt.date.today()
    os.makedirs(args.outdir, exist_ok=True)
    log = lambda s: print(s, file=sys.stderr)                        # noqa: E731

    # ⚠️ resolve_deldate 也会请求接口（日期探测），同样可能被限流。
    # 必须一起包在 try 里 —— 否则 BusyError 会以 traceback 形式抛出、退出码变成 1，
    # 而 run.sh / 自动化都按"退出码 3 = 限流"来判定。
    try:
        dates = resolve_deldate(today, parse_dates_arg(args.dates))
    except BusyError as e:
        print("ERROR: %s（已中断，避免加重封禁）" % e, file=sys.stderr)
        sys.exit(3)
    log("删除日期：" + "、".join("%s=%s" % (k, v[0]) for k, v in dates.items()))

    pool = {}
    try:
        # ⚠️ 顺序有意义：**先枚举 ≤4 位的小池子**，再花剩余预算去采样大池子。
        # 反过来的话，大池子采样会把请求预算吃光，把 top ≤4 位（今天只有 81 条、
        # 却是最值钱的一档）整段跳掉 —— 2026-09-26 就这么把 loho.top 挤掉了。
        pool_totals = fetch_small_pools(dates, pool, log)
        pool_totals.update(fetch_scoped(dates, pool, log))
    except BusyError as e:
        print("ERROR: %s（已中断，避免加重封禁）" % e, file=sys.stderr)
        sys.exit(3)

    # ── 保险丝：只保留"记录自带 deldate == 该后缀目标删除日期"的项 ──
    # 抓取已经改成单后缀分块了，正常不会再有混批次；但这是**必须保留的兜底**：
    # 2026-09-26 的事故就是跨批次记录混入，导致报告写着 top=10-01、榜单却是 09-30 的米。
    keep, dropped = {}, []
    for d, it in pool.items():
        ext = (it.get("domext") or d.rsplit(".", 1)[-1] or "").lower()
        want = dates.get(ext, (None,))[0]
        got = (it.get("deldate") or "")[:10]
        if want and got == want:
            keep[d] = it
        else:
            dropped.append((d, got, want))
    if dropped:
        log("  ✂ 剔除 %d 条非本批次记录（例：%s）"
            % (len(dropped), "、".join("%s[%s≠%s]" % t for t in dropped[:3])))
    pool = keep

    # ── 溢价域名整段排除 ──
    # 用户 2026-09-26 明确说："结果里也不需要溢价域名"（溢价米预订要额外付费，他不会考虑注册）。
    # 所以不是"排在后面"，是**直接不出现**。排除数量仍然写进报告，避免看起来像漏查。
    excluded_premium = [d for d, it in pool.items() if it.get("ispremium")]
    for d in excluded_premium:
        pool.pop(d, None)
    if excluded_premium:
        log("  💸 排除 %d 个溢价域名（不考虑注册）：%s"
            % (len(excluded_premium), "、".join(sorted(excluded_premium)[:6])))

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

    # ── 四声母（拼音首字母）诊断 ──
    # 用户 2026-09-26 问："拼音首字母（4声母）这几天都没有取到，是没有好的值得推荐，还是查询有问题？"
    # 实测答案：机制没问题，是白名单太窄 —— 今天抽样的 100 个 4 位 .cn 里 79 个都是
    # "读不出来的串"（正是四声母的形态），但一条都没落在 210 个好词组合里。
    # 所以把这个数字**每天写进报告**：以后一眼就能看出是"真没有"还是"没查到"。
    def _lbl(dom):
        return dom.split(".")[0]
    four = [d for d in pool if re.fullmatch(r"[a-z]{4}", _lbl(d))]
    hits = sorted(d for d in four if lang.abbr4_match(_lbl(d)))
    abbr_diag = {"four_total": len(four), "four_whitelisted": len(hits),
                 "whitelist": lang.abbr4_size(), "hits": hits[:10]}
    log("  🔤 四声母诊断：本批次 4 位域名 %d 个，命中好词表 %d 个（好词表 %d 个组合）"
        % (len(four), len(hits), abbr_diag["whitelist"]))

    by_cls = {k: [x for x in scored if x["cls"] == k] for k in lang.CLASS_ORDER}
    result = {
        "date": today.isoformat(), "dates": dates,
        "pool_totals": pool_totals,
        "pool_total": sum(scope_of(v)[0] for v in pool_totals.values()),
        "dropped_offbatch": len(dropped),
        "excluded_premium": len(excluded_premium),
        "abbr4_diag": abbr_diag,
        "sampled": len(items), "all_items": scored, "top10": scored[:10],
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
