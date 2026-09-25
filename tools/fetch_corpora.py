#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""下载构建语言索引所需的公开语料（约 22MB），然后由 build_assets.py 生成运行时索引。

之所以不在部署包里塞这些语料：ime-base.dict.yaml 有 16MB，塞进来部署包会变得很大。
这些源都是公开的，直接从 jsdelivr 拉（失败则回退 raw.githubusercontent.com）。

用法：python3 fetch_corpora.py [--force]
"""

import argparse
import os
import ssl
import sys
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ASSETS = os.path.join(ROOT, "assets")

CANDS = {
    "pinyin.txt": [
        "https://cdn.jsdelivr.net/gh/mozillazg/pinyin-data@master/pinyin.txt",
        "https://raw.githubusercontent.com/mozillazg/pinyin-data/master/pinyin.txt",
    ],
    "ime-base.dict.yaml": [
        "https://cdn.jsdelivr.net/gh/iDvel/rime-ice@main/cn_dicts/base.dict.yaml",
        "https://raw.githubusercontent.com/iDvel/rime-ice/main/cn_dicts/base.dict.yaml",
    ],
    "words_alpha.txt": [
        "https://cdn.jsdelivr.net/gh/dwyl/english-words@master/words_alpha.txt",
        "https://raw.githubusercontent.com/dwyl/english-words/master/words_alpha.txt",
    ],
    "google-20k.txt": [
        "https://cdn.jsdelivr.net/gh/first20hours/google-10000-english@master/20k.txt",
        "https://raw.githubusercontent.com/first20hours/google-10000-english/master/20k.txt",
    ],
    "google-10000-english-no-swears.txt": [
        "https://cdn.jsdelivr.net/gh/first20hours/google-10000-english@master/google-10000-english-no-swears.txt",
        "https://raw.githubusercontent.com/first20hours/google-10000-english/master/google-10000-english-no-swears.txt",
    ],
}

MIN_SIZE = {"pinyin.txt": 500_000, "ime-base.dict.yaml": 2_000_000,
            "words_alpha.txt": 2_000_000, "google-20k.txt": 50_000,
            "google-10000-english-no-swears.txt": 20_000}


def fetch(name, urls, force=False):
    dst = os.path.join(ASSETS, name)
    if not force and os.path.exists(dst) and os.path.getsize(dst) >= MIN_SIZE.get(name, 1):
        print("  [跳过] %-36s 已存在 %d 字节" % (name, os.path.getsize(dst)))
        return True
    ctx = ssl.create_default_context()
    for u in urls:
        try:
            req = urllib.request.Request(u, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=180, context=ctx) as r:
                data = r.read()
            if len(data) < MIN_SIZE.get(name, 1):
                print("  [太小] %-36s %s -> %d 字节" % (name, u.split("/")[2], len(data)))
                continue
            with open(dst, "wb") as f:
                f.write(data)
            print("  [完成] %-36s %d 字节  <- %s" % (name, len(data), u.split("/")[2]))
            return True
        except Exception as e:                                        # noqa: BLE001
            print("  [失败] %-36s %s: %s" % (name, u.split("/")[2], str(e)[:70]))
    print("  [放弃] %s —— 所有镜像都拿不到" % name, file=sys.stderr)
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()
    os.makedirs(ASSETS, exist_ok=True)
    print("下载语料到", ASSETS)
    bad = [n for n, u in CANDS.items() if not fetch(n, u, args.force)]
    if bad:
        print("\n以下语料下载失败：%s" % "、".join(bad), file=sys.stderr)
        print("可以手动用 scp 把对应文件放到 assets/ 目录后重跑本脚本（会跳过已存在的）。",
              file=sys.stderr)
        sys.exit(1)
    print("全部语料就绪。")


if __name__ == "__main__":
    main()
