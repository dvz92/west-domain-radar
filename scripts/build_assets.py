#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
构建语言索引（只需跑一次；语料更新时重跑）。

输入（assets/）：
  pinyin.txt             mozillazg/pinyin-data  汉字→拼音
  ime-base.dict.yaml     rime-ice 词库           词语<TAB>拼音<TAB>权重
  idiom.json             chinese-xinhua          成语（含 abbreviation 拼音首字母）
  google-20k.txt         英文词频 top2w
  words_alpha.txt        英文词库（37w）

输出（assets/）：
  syllables.txt            合法拼音音节表（去声调）
  cn_pinyin_words.txt      拼音串<TAB>词频权重<TAB>代表词   （1~3 音节词）
  abbr4.txt                四字词首字母<TAB>中文词<TAB>排序权重
  en_trigram.json          英文三元组计数（用于发音可读性打分）

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


def build_idioms(word_pinyin):
    """四字词首字母索引：**只收人工挑选的"好词"**（assets/good4.txt）

    为什么这么做：
      - 成语库/输入法词库里有大量生僻或语义不好的四字词（室迩人遥、造谣惑众、油尽灯枯、
        旅游路线、单位负责……），只靠"词频阈值"筛不掉，用户明确说这些是"硬凑的、没意思"。
      - 所以改成**人工词表白名单**：只保留吉祥/正面/像品牌的常用四字词。
        宁可少，也不要脏——用户原话："大部分都不行，看都不用看"。
    """
    out = os.path.join(ASSETS, "abbr4.txt")
    words = []
    for line in _read_lines_local("good4.txt"):
        for tok in line.replace("　", " ").split():
            w = tok.strip()
            if len(w) == 4 and all("\u4e00" <= ch <= "\u9fff" for ch in w) and w not in words:
                words.append(w)

    abbr, missing = defaultdict(list), []
    for w in words:
        py = word_pinyin.get(w)
        if not py:
            missing.append(w)
            continue
        syls = py.split()
        if len(syls) != 4 or not all(re.fullmatch(r"[a-z]+", s) for s in syls):
            missing.append(w)
            continue
        abbr["".join(s[0] for s in syls)].append(w)

    with open(out, "w", encoding="utf-8") as f:
        for a, lst in sorted(abbr.items()):
            f.write("%s\t%s\n" % (a, "\t".join(sorted(set(lst)))))
    print("  abbr4.txt            %d 组首字母 / %d 个词（词库缺拼音 %d 个：%s）"
          % (len(abbr), sum(len(v) for v in abbr.values()), len(missing), "、".join(missing[:8])))


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
    build_idioms(word_pinyin)
    build_trigram()
    build_clusters()
    print("完成。")
