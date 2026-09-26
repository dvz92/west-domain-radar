#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
域名语言分类：判断一个域名主体属于哪一类，决定它的"关注价值层级"。

优先级（高 → 低，按用户明确给出的偏好）：
  C1 英文单词      有实义的英文词（在英文词频表内）
  C2 可发音英文    不是词，但读得出来（音位结构 + 三元组发音模型）
  C3 双拼/三拼     能完整拆成 2~3 个合法拼音音节；若是真实中文词语再加分
  C4 拼音首字母    4 位首字母能对上一个**常用**中文四字词（对不上则直接淘汰）
  None             以上都不是 → 不关注（如 rpkc.cn）

分类判定顺序是 C1 → C3 → C2 → C4：
  "能拆成拼音"的字符串人对它的第一读感就是拼音（ansu→双拼），
  哪怕它同时也满足"可发音英文"，也应归到双拼。但**打分**上 C2 > C3，
  所以真正的"英文感"域名（kombio、sery 拆不出合法拼音）仍排在双拼前面。

依赖 assets/ 下由 build_assets.py 生成的索引。
"""

import json
import math
import os
import re

ASSETS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets")

CLASS_ORDER = ["en_word", "en_pron", "pinyin", "pinyin_abbr"]
CLASS_LABEL = {
    "en_word": "英文单词",
    "en_pron": "可发音英文",
    "pinyin": "双拼/三拼",
    "pinyin_abbr": "拼音首字母",
}

_PRON_THRESHOLD = -4.6        # 三元组平均 log10 概率下限（已用样本校准）
_vowels = set("aeiou")

# ------------------------------------------------- 品牌感（2026-09-25 用用户样例校准）
# 用户给出的品味样例：sioly / rofar / kaote / atiron / korp / fary / mexa / rady
# 加上此前确认喜欢的：kombio / sery / glax
# 共同特征（这条才是"像不像用户会看上的米"的关键，三元组模型几乎区分不出来）：
#   ① 元音组数 ≥2（4 位的短名可以是 1 组）
#   ② 词尾"开音节"或响音 —— 以 a/e/i/o/u/y 收尾最优，r/n/l/m 次之，
#      元音 + r + 辅音（korp 这种 r 化元音，如 corp/warp）再之
#   ③ 辅音与元音尽量交替（CVCV / CVCVC）
#   ④ 不能有不合法的辅音簇（词首 sf/sc 之外、词尾 gr/ls/ds 这类硬簇）
# 反例（用户此前明确否掉的）：ecotte / conalmat / trodesi 这类 7~8 位字母堆砌，
# 以及 duls / sfad / cods / higr / scah / dmon / rpkc 这类辅音过密、读感生硬的串。
_BRAND_ONSET2 = {"bl", "br", "cl", "cr", "dr", "fl", "fr", "gl", "gr", "pl", "pr",
                 "sc", "sk", "sl", "sm", "sn", "sp", "st", "sw", "tr", "tw",
                 "th", "sh", "ch", "wh", "ph", "qu", "kn", "wr", "gn", "ps", "sq"}
_BRAND_COD1 = set("bcdfgklmnprstxz")          # 英文里能单独收尾的辅音
_BRAND_COD2 = {"nt", "nd", "st", "rt", "rd", "rk", "rp", "rl", "rm", "rn",
               "lt", "ld", "lk", "lp", "lf", "lm", "mp", "mb", "nk", "ng",
               "sk", "sp", "ct", "pt", "ft", "xt", "ck",
               "ll", "ss", "ff", "tt", "dd", "mm", "nn", "pp", "bb", "gg", "zz"}
_BRAND_VY = set("aeiouy")
_BRAND_SOFT1 = set("aeiouy")                  # 最理想：开音节收尾
_BRAND_SOFT07 = set("rnlm")                   # 次之：响音收尾
_BRAND_MIN_WORD = 55.0                        # 非标准双拼要达到这个品牌感才按"可发音英文"处理


def brandability(label):
    """品牌感 0~100。越高越像用户会看上的域名主体（按他的样例校准，不是语言学指标）。"""
    label = (label or "").lower()
    if not re.fullmatch(r"[a-z]+", label):
        return 0.0
    # r 化元音：V+r 后面不再接元音时，"or/ar/er" 算一个元音单位（korp 里的 or，同 corp/warp）
    core = re.sub(r"([aeiou])r(?![aeiouy])", r"\1", label)
    groups = re.findall(r"[aeiouy]+", core)
    if not groups:
        return 0.0
    g = len(groups)

    clusters = re.findall(r"[^aeiouy]+", core)
    onset = clusters[0] if (clusters and core[0] not in _BRAND_VY) else ""
    coda = clusters[-1] if (clusters and core[-1] not in _BRAND_VY) else ""
    pen = 0.0
    for c in clusters:
        if c == onset:
            if len(c) >= 2 and c not in _BRAND_ONSET2:
                pen += 20                                  # 非法词首簇：sfad / xlylem
        elif c == coda:
            if len(c) >= 2 and c not in _BRAND_COD2:
                pen += 12                                  # 非法词尾簇：higr / duls
            elif len(c) == 1 and c not in _BRAND_COD1:
                pen += 10                                  # 不能收尾的单辅音：scah
        elif len(c) >= 2 and c not in _BRAND_COD2:
            pen += 10                                      # 词中硬簇

    alt = sum(1 for i in range(len(core) - 1)
              if (core[i] in _BRAND_VY) != (core[i + 1] in _BRAND_VY)) / max(1, len(core) - 1)

    last = label[-1]
    if last in _BRAND_SOFT1:
        soft = 1.0
    elif last in _BRAND_SOFT07:
        soft = 0.7
    elif len(label) >= 3 and label[-2] == "r" and label[-3] in "aeiou":
        soft = 0.6                                         # korp 这类 r 化元音收尾
    else:
        soft = 0.0

    s = 18.0 * min(g, 3) + 17.0 * soft + 14.0 * alt - pen
    if len(label) not in (4, 5, 6):
        s -= 15.0                                          # 用户明确不要 7~8 位与 3 位
    return max(0.0, min(100.0, s))

_syllables = _pinyin_words = _abbr4 = _trigram = _common_en = None
_onsets = _codas = None
_total_tri = 0
_vocab = 28
_words_alpha = None


def _read_lines(name):
    p = os.path.join(ASSETS, name)
    if not os.path.exists(p):
        return []
    with open(p, "r", encoding="utf-8", errors="ignore") as f:
        return [l.rstrip("\n") for l in f if l.strip()]


def load():
    global _syllables, _pinyin_words, _abbr4, _trigram, _common_en
    global _onsets, _codas, _total_tri
    if _syllables is not None:
        return
    _syllables = set(_read_lines("syllables.txt"))

    _pinyin_words = {}
    for line in _read_lines("cn_pinyin_words.txt"):
        parts = line.split("\t")
        if len(parts) >= 3 and parts[1].isdigit():
            _pinyin_words[parts[0]] = (int(parts[1]), parts[2])

    _abbr4 = {}
    for line in _read_lines("abbr4.txt"):
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        cands = []
        for tok in parts[1:]:
            if ":" in tok:                      # 旧格式 词:权重
                w, wt = tok.rsplit(":", 1)
                cands.append((int(wt) if wt.isdigit() else 0, w))
            elif tok:
                cands.append((0, tok))          # 人工词表格式（无权重）
        if cands:
            cands.sort(key=lambda x: (-x[0], x[1]))
            _abbr4[parts[0]] = cands

    _common_en = {}
    for i, line in enumerate(_read_lines("google-20k.txt")):
        w = line.strip().lower()
        if w.isalpha():
            _common_en.setdefault(w, i + 1)
    for i, line in enumerate(_read_lines("google-10000-english-no-swears.txt")):
        w = line.strip().lower()
        if w.isalpha():
            _common_en.setdefault(w, i + 1)

    p = os.path.join(ASSETS, "en_trigram.json")
    _trigram = json.load(open(p, encoding="utf-8")) if os.path.exists(p) else {}
    _total_tri = sum(_trigram.values())

    p = os.path.join(ASSETS, "en_clusters.json")
    if os.path.exists(p):
        d = json.load(open(p, encoding="utf-8"))
        _onsets, _codas = set(d.get("onsets") or []), set(d.get("codas") or [])
    else:
        _onsets, _codas = set(), set()


def _words_set():
    global _words_alpha
    if _words_alpha is None:
        _words_alpha = set()
        p = os.path.join(ASSETS, "words_alpha.txt")
        if os.path.exists(p):
            with open(p, "r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    w = line.strip().lower()
                    if 2 <= len(w) <= 14 and w.isalpha():
                        _words_alpha.add(w)
    return _words_alpha


# ------------------------------------------------------------ 英文
def en_rank(label):
    """常见英文词频排名；不是常见词返回 None"""
    load()
    return _common_en.get(label)


def is_obscure_en(label):
    """在 37 万英文词库里但不在常见词表内"""
    return label in _words_set()


def pronounce_score(label):
    """三元组发音可读性：平均 log10 概率，越高越像英文"""
    load()
    if not _trigram:
        return 0.0
    s = "^" + label + "$"
    denom = _total_tri + 0.5 * (_vocab ** 3)
    tot, n = 0.0, 0
    for i in range(len(s) - 2):
        tot += math.log10((_trigram.get(s[i:i + 3], 0) + 0.5) / denom)
        n += 1
    return tot / n if n else -99.0


def _lead_consonants(w):
    i = 0
    while i < len(w) and w[i] not in _vowels:
        i += 1
    return w[:i]


def _tail_consonants(w):
    j = len(w)
    while j > 0 and w[j - 1] not in _vowels:
        j -= 1
    return w[j:]


def is_pronounceable(label):
    load()
    if not re.fullmatch(r"[a-z]{3,8}", label):
        return False
    if not re.search(r"[aeiou]", label):                  # 必须有元音
        return False
    if re.search(r"(.)\1\1", label):                      # 连续 3 个相同字母
        return False
    if re.search(r"[bcdfghjklmnpqrstvwxz]{4,}", label):   # 4 连辅音
        return False
    if len(re.findall(r"[aeiou]+", label)) > 4:           # 音节数过多
        return False
    onset, coda = _lead_consonants(label), _tail_consonants(label)
    if onset and onset not in _onsets:                    # 词首辅音簇必须合法（挡掉 dmon/llabk）
        return False
    if coda and coda not in _codas:
        return False
    if is_obscure_en(label):                              # 本身就是（冷僻）英文词
        return True
    return pronounce_score(label) >= _PRON_THRESHOLD


# ------------------------------------------------------------ 拼音
def pinyin_split(label, max_syl=3):
    """把 label 完整拆成 1~max_syl 个合法拼音音节；优先音节数少的方案"""
    load()
    n = len(label)
    if n == 0 or n > 12:
        return None
    best = [None]

    def dfs(i, acc):
        if best[0] is not None and len(acc) >= len(best[0]):
            return
        if i == n:
            if best[0] is None or len(acc) < len(best[0]):
                best[0] = list(acc)
            return
        if len(acc) >= max_syl:
            return
        for j in range(min(n, i + 6), i, -1):
            if label[i:j] in _syllables:
                acc.append(label[i:j])
                dfs(j, acc)
                acc.pop()
                if best[0] is not None:
                    return

    dfs(0, [])
    return best[0]


def pinyin_word(syls):
    """音节组合是否为真实中文词语 → (词频权重, 代表词) 或 None"""
    load()
    if not syls:
        return None
    return _pinyin_words.get("".join(syls))


def abbr4_match(label):
    """4 位拼音首字母 → 常用中文四字词列表 [(权重, 词)]，无匹配返回 None"""
    load()
    if not re.fullmatch(r"[a-z]{4}", label):
        return None
    return _abbr4.get(label)


def abbr4_size():
    """白名单里有多少个四字词首字母组合（报告里用来说明命中率）"""
    load()
    return len(_abbr4)


# ------------------------------------------------------------ 主分类
def classify(label):
    """返回 dict(cls, score_hint, detail, reason)，或 None 表示不值得关注"""
    label = (label or "").lower()
    if not re.fullmatch(r"[a-z]{2,12}", label):
        return None

    # C1 英文单词（有实义 = 在英文词频表内）
    rank = en_rank(label)
    if rank is not None and len(label) >= 3:
        tier = "高频英文词" if rank <= 1000 else "常用英文词" if rank <= 5000 else "英文单词"
        return dict(cls="en_word", score_hint=100 + (20 if rank <= 1000 else 10 if rank <= 5000 else 0),
                    detail=dict(rank=rank), reason=tier)

    # C3 拼音（判定优先于 C2，但打分低于 C2）
    syls = pinyin_split(label)
    if syls:
        pword = pinyin_word(syls)
        kind = "单拼" if len(syls) == 1 else ("双拼" if len(syls) == 2 else "三拼")
        if pword and len(label) >= 4:
            wt, w = pword
            bonus = 8 if wt >= 3000 else 5 if wt >= 500 else 2 if wt >= 50 else 0
            return dict(cls="pinyin", score_hint=45 + bonus,
                        detail=dict(syllables=syls, word=w, weight=wt),
                        reason="%s → %s" % (kind, w))
        if len(syls) == 1 and len(label) >= 3:
            return dict(cls="pinyin", score_hint=52, detail=dict(syllables=syls), reason="单拼")
        # 拆得出音节但**不是真实词语** → 它其实只是"一串字母"，
        # 到底算拼音还是英文，由品牌感决定（用户把 kaote 归到"可发音英文"那一档）。
        # ⚠️ 必须同时确认"它确实过得了 C2 的发音检查"，否则会掉到 C4 被误淘汰。
        _b = brandability(label)
        _can_en = (4 <= len(label) <= 6) and is_pronounceable(label)
        if 2 <= len(syls) <= 3 and len(label) >= 4:
            if _b < _BRAND_MIN_WORD or not _can_en:
                return dict(cls="pinyin", score_hint=42 if len(syls) == 2 else 36,
                            detail=dict(syllables=syls, brand=round(_b, 1)),
                            reason=kind + "（非标准词语）")
        elif len(syls) == 1 and len(label) >= 3:
            if _b < _BRAND_MIN_WORD or not _can_en:
                return dict(cls="pinyin", score_hint=52,
                            detail=dict(syllables=syls, brand=round(_b, 1)), reason="单拼")

    # C2 可发音英文 —— 只收 4~6 位。
    # 用户明确不要 7~8 位的"随机拼接但读得出来"的名字（ecotte/conalmat/trodesi 那类），
    # 那批只是字母组合，没有品牌感；6 位已经是上限（kombio 正好 6 位）。
    if 4 <= len(label) <= 6 and is_pronounceable(label):
        ps = pronounce_score(label)
        br = brandability(label)
        tri = max(0.0, min(1.0, (ps + 5.0) / 2.5))          # 三元组归一到 0~1
        # ⚠️ 主序是品牌感（用户的品味），三元组只做小幅微调 ±6。
        # 只用三元组会出错：实测 ised(82.0) > atiron(81.3)、duls(78.0) > rofar(77.2)。
        return dict(cls="en_pron",
                    score_hint=40 + 0.5 * br + 6.0 * tri,
                    detail=dict(pron=round(ps, 3), brand=round(br, 1),
                                obscure=is_obscure_en(label),
                                syllables=(syls if syls else None)),
                    reason="可发音英文")

    # C4 拼音首字母（必须对得上常用中文四字词，否则淘汰）
    m = abbr4_match(label)
    if m:
        return dict(cls="pinyin_abbr", score_hint=25,
                    detail=dict(matches=m), reason="拼音首字母 → " + m[0][1])

    return None


if __name__ == "__main__":
    # 正样本 = 用户 2026-09-25 给的品味样例 + 此前确认喜欢的
    POS = ("sioly", "rofar", "kaote", "atiron", "korp", "fary", "mexa", "rady",
           "kombio", "sery", "glax")
    # 负样本 = 用户否掉的 / 此前把榜单污染了的
    NEG = ("blap", "duls", "gerv", "faex", "sfad", "higr", "cods", "scah",
           "ziin", "dmon", "jxggw", "rpkc", "llabk", "mpzhcn")
    MID = ("vaky", "enop", "ised", "amov", "eler", "ansu", "baidu", "xrmm",
           "bros", "lawter", "renn", "qiza")
    for title, group in (("=== 正样本（用户的品味）===", POS),
                         ("=== 负样本（应低分 / 淘汰）===", NEG),
                         ("=== 中间地带（结构合法但一般）===", MID)):
        print(title)
        for w in group:
            r = classify(w)
            if r:
                print("  %-8s %-11s %6.1f  %s" % (w, r["cls"], r["score_hint"], r["reason"]))
            else:
                print("  %-8s %-11s %6s  ✗ 淘汰" % (w, "-", "-"))
        print()
