#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
构建语言索引（只需跑一次；语料更新时重跑）。

输入（assets/）：
  pinyin.txt             mozillazg/pinyin-data  汉字→拼音
  ime-base.dict.yaml     rime-ice 词库           词语<TAB>拼音<TAB>权重（含大量成语与生活常用词）
  google-20k.txt         英文词频 top2w
  words_alpha.txt        英文词库（37w）
  good4.txt              人工精选的四字好词（可选，会被当作最高优先候选）

输出（assets/）：
  syllables.txt            合法拼音音节表（去声调）
  cn_pinyin_words.txt      拼音串<TAB>词频权重<TAB>代表词   （1~3 音节词）
  abbr4.txt                四字词首字母<TAB>词:权重<TAB>…（人工精选 + IME 常用词，见 build_abbr4）
  en_trigram.json          英文三元组计数（用于发音可读性打分）
  en_clusters.json         英文词首/词尾辅音簇

用法：python build_assets.py
"""

import json
import os
import re
import sys
import unicodedata
from collections import Counter, defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
ASSETS = os.path.join(os.path.dirname(HERE), "assets")

TONE = re.compile(r"[1-5]$")


def build_syllables():
    """从 pinyin.txt 抽合法音节（去声调/声调符号）
    文件格式： U+3007: líng,yuán,xīng  # 〇   —— 拼音是带声调符号的，需 NFD 去组合符
    """
    src = os.path.join(ASSETS, "pinyin.txt")
    syls = set()
    with open(src, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            line = line.split("#", 1)[0].strip()
            if not line or line.startswith("#") or ":" not in line:
                continue
            _, val = line.split(":", 1)
            for p in val.split(","):
                p = p.strip().lower()
                if not p:
                    continue
                p = unicodedata.normalize("NFD", p)
                p = "".join(ch for ch in p if not unicodedata.combining(ch))
                p = p.replace("ü", "v").replace("u:", "v")
                p = TONE.sub("", p)
                if re.fullmatch(r"[a-z]+", p):
                    syls.add(p)
    out = os.path.join(ASSETS, "syllables.txt")
    with open(out, "w", encoding="utf-8") as f:
        f.write("\n".join(sorted(syls)))
    print("  syllables.txt        %d 个音节" % len(syls))
    return syls


def build_cn_words():
    """IME 词库 → 拼音串->(权重, 代表词)，并顺带产出四字词首字母索引"""
    src = os.path.join(ASSETS, "ime-base.dict.yaml")
    best = {}                       # pinyin_join -> (weight, word)
    abbr = defaultdict(list)        # 首字母 -> [(weight, word)]
    wpy = {}                        # 词语 -> 拼音（供人工词表 good4.txt 查拼音用）
    n = 0
    with open(src, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            if not line or line.startswith("#") or line.startswith("---") or line.startswith("..."):
                continue
            parts = line.rstrip("\n").split("\t")
            if len(parts) < 2:
                continue
            word, py = parts[0].strip(), parts[1].strip()
            if not word or not py or " " not in py:
                continue
            syls = py.split()
            if not (2 <= len(syls) <= 4):
                continue
            if not all(re.fullmatch(r"[a-z]+", s) for s in syls):
                continue
            try:
                w = int(parts[2]) if len(parts) > 2 and parts[2].strip().isdigit() else 1
            except ValueError:
                w = 1
            n += 1
            if len(word) == 4 and len(syls) == 4:
                wpy.setdefault(word, py)
            if len(syls) <= 3:
                key = "".join(syls)
                if key not in best or w > best[key][0]:
                    best[key] = (w, word)
            if len(syls) == 4 and len(word) == 4:
                ini = "".join(s[0] for s in syls)
                if re.fullmatch(r"[a-z]{4}", ini):
                    abbr[ini].append((w, word))

    out = os.path.join(ASSETS, "cn_pinyin_words.txt")
    with open(out, "w", encoding="utf-8") as f:
        for k, (w, word) in sorted(best.items()):
            f.write("%s\t%d\t%s\n" % (k, w, word))
    print("  cn_pinyin_words.txt  %d 条拼音词（源 %d 行）" % (len(best), n))
    return best, abbr, wpy


# ---------------------------------------------------------------- 四声母表
# 词表规模直接决定"四声母"能不能捞到东西。2026-09-26 用户反馈：
#   "4声母的词库太少了，只有210个，这怎么可能找到，光成语就不止210个了吧，
#    还有生活中常用的4字词，比如 plmm 漂亮妹妹、ytdw 一天到晚等等。"
# 旧实现只认人工表 good4.txt（240 词 → 210 组合），命中率 210/26⁴ = 0.046%，几天都是 0。
# 现在改成三层：
#   1) good4.txt 人工精选 —— 权重加 CURATED_BOOST，永远排在候选词第一位
#   2) IME 词库（rime-ice，含大量成语 + 生活口语词）中 weight ≥ ABBR4_MIN_WEIGHT 的四字词
#   3) 坏词过滤：负面/不雅字、行政商务套话、虚词结尾
# 不额外引入 3 万条生僻成语库 —— IME 已经覆盖成语（抽查 一帆风顺/海纳百川/上善若水 都在），
# 而 chinese-xinhua 那类库里"室迩人遥"这种生僻成语正是用户明确讨厌的。
ABBR4_MIN_WEIGHT = 1000
CURATED_BOOST = 10 ** 9

BAD_CHARS = set("死不病鬼骗偷杀凶丧烂贱骚淫赌穷债坑冤牢疯傻蠢丑恶劣恨仇骂抢盗假伪毒灾祸衰败哭"
                "烟酒娼妓贼匪瘟癌残障瞎聋哑弃逃亡殁殡")
BAD_SUBSTR = ("公司", "有限", "股份", "集团", "银行", "保险", "权限", "时间", "联系", "方式", "版权",
              "责任", "部门", "网站", "系统", "服务", "管理", "电话", "地址", "邮箱", "下载", "注册",
              "发布", "更新", "在线", "用户", "密码", "论坛", "帖子", "积分", "等级", "经验", "主题",
              "回复", "查看", "附件", "数据", "信息", "平台", "中心", "市场", "经济", "政府", "人民",
              "工作", "会议", "文件", "通知", "报告", "项目", "产品", "价格", "费用", "金额", "订单",
              "支付", "账户", "余额", "版本", "功能", "设置", "选项", "问题", "方法", "情况", "内容",
              "题目", "答案", "考试", "学校", "老师", "学生", "医院", "医生", "药品", "法律", "法规",
              "政策", "规定", "标准", "技术", "工程", "设备", "材料", "能源", "交通", "运输", "图片",
              "来源", "帖子", "本页", "版主", "主管", "代表", "主义", "环境", "方案", "引擎", "编码")
BAD_TAIL = tuple("的了着呢吧吗啊呀啦喔哦")
BAD_HEAD = ("在", "把", "被", "让", "对", "从", "是", "有", "和", "与", "为")


def _iter_ime_four(path):
    """从 IME 词库取「4 个字 + 4 个音节」的词 → (权重, 词, 首字母)"""
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            if not line or line.startswith("#") or line.startswith("---") or line.startswith("..."):
                continue
            parts = line.rstrip("\n").split("\t")
            if len(parts) < 2:
                continue
            word, py = parts[0].strip(), parts[1].strip()
            if len(word) != 4 or not py or " " not in py:
                continue
            syls = py.split()
            if len(syls) != 4 or not all(re.fullmatch(r"[a-z]+", s) for s in syls):
                continue
            ini = "".join(s[0] for s in syls)
            if not re.fullmatch(r"[a-z]{4}", ini):
                continue
            try:
                w = int(parts[2]) if len(parts) > 2 and parts[2].strip().isdigit() else 1
            except ValueError:
                w = 1
            yield w, word, ini


def _bad(word):
    if any(ch in BAD_CHARS for ch in word):
        return True
    if word.endswith(BAD_TAIL) or word.startswith(BAD_HEAD):
        return True
    return any(b in word for b in BAD_SUBSTR)


def build_abbr4(word_pinyin, min_weight=ABBR4_MIN_WEIGHT):
    """四字词首字母索引：人工精选 + IME 常用词（成语/生活口语都在里面）

    good4.txt 里的词权重加 CURATED_BOOST，永远排在各组合候选词的第一位 ——
    报告里显示的就是人工精选的那个词，而不是同组合里某个平庸的词。
    """
    cands = defaultdict(list)          # ini -> [(weight, word)]
    curated = set()

    # 1) 人工精选（顶层）
    for line in _read_lines_local("good4.txt"):
        for tok in line.replace("　", " ").split():
            w = tok.strip()
            if len(w) != 4 or not all("\u4e00" <= ch <= "\u9fff" for ch in w) or w in curated:
                continue
            py = word_pinyin.get(w)
            if not py:
                continue
            syls = py.split()
            if len(syls) != 4 or not all(re.fullmatch(r"[a-z]+", s) for s in syls):
                continue
            ini = "".join(s[0] for s in syls)
            if re.fullmatch(r"[a-z]{4}", ini):
                curated.add(w)
                cands[ini].append((CURATED_BOOST + 1, w))

    # 2) IME 词库（成语 + 生活常用四字词）
    n_ime = skipped = 0
    for w, word, ini in _iter_ime_four(os.path.join(ASSETS, "ime-base.dict.yaml")):
        if w < min_weight or word in curated:
            continue
        if _bad(word):
            skipped += 1
            continue
        cands[ini].append((w, word))
        n_ime += 1

    out = os.path.join(ASSETS, "abbr4.txt")
    with open(out, "w", encoding="utf-8") as f:
        for ini, lst in sorted(cands.items()):
            # 同组合内按权重降序；词去重；只留前 6 个（报告里也就显示前几个）
            seen, keep = set(), []
            for w_, word in sorted(lst, key=lambda x: (-x[0], x[1])):
                if word in seen:
                    continue
                seen.add(word)
                keep.append((w_, word))
                if len(keep) >= 6:
                    break
            f.write("%s\t%s\n" % (ini, "\t".join("%s:%d" % (word, w_) for w_, word in keep)))
    print("  abbr4.txt            %d 组首字母 / %d 个词"
          "（人工精选 %d + IME weight≥%d 的 %d，过滤掉坏词 %d）"
          % (len(cands), sum(len(v) for v in cands.values()), len(curated),
             min_weight, n_ime, skipped))
    return len(cands)


def _read_lines_local(name):
    p = os.path.join(ASSETS, name)
    if not os.path.exists(p):
        return []
    with open(p, "r", encoding="utf-8", errors="ignore") as f:
        return [l.rstrip("\n") for l in f if l.strip()]


def build_clusters():
    """英文词首/词尾辅音簇集合（音位结构约束，用于发音可读性判定）"""
    from collections import Counter as C2
    VOW = set("aeiou")
    ons, cod = C2(), C2()
    src = os.path.join(ASSETS, "words_alpha.txt")
    with open(src, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            w = line.strip().lower()
            if not (3 <= len(w) <= 14) or not w.isalpha():
                continue
            i = 0
            while i < len(w) and w[i] not in VOW:
                i += 1
            if i:
                ons[w[:i]] += 1
            j = len(w)
            while j > 0 and w[j - 1] not in VOW:
                j -= 1
            if j < len(w):
                cod[w[j:]] += 1
    data = {"onsets": sorted(k for k, v in ons.items() if v >= 3),
            "codas": sorted(k for k, v in cod.items() if v >= 3)}
    out = os.path.join(ASSETS, "en_clusters.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(data, f)
    print("  en_clusters.json     词首 %d / 词尾 %d 个辅音簇"
          % (len(data["onsets"]), len(data["codas"])))


def build_trigram():
    """英文三元组计数（用于发音可读性）"""
    src = os.path.join(ASSETS, "google-20k.txt")
    cnt = Counter()
    with open(src, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            w = line.strip().lower()
            if not (3 <= len(w) <= 12) or not w.isalpha():
                continue
            s = "^" + w + "$"
            for i in range(len(s) - 2):
                cnt[s[i:i + 3]] += 1
    out = os.path.join(ASSETS, "en_trigram.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(cnt, f)
    print("  en_trigram.json      %d 个三元组" % len(cnt))


if __name__ == "__main__":
    print("构建语言索引 ->", ASSETS)
    build_syllables()
    _best, _abbr, word_pinyin = build_cn_words()
    build_abbr4(word_pinyin)
    build_trigram()
    build_clusters()
    print("完成。")
