#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""仓库自测：把仓库当成"已安装"的目录，把管理面板的所有命令跑一遍。

不联网、不发邮件、不碰真实 crontab（用 WDRADAR_CRONTAB_FILE 落到临时文件）。

用法：
    python3 dev/selftest.py [仓库路径]
"""

import os
import shutil
import subprocess
import sys
import tempfile

sys.stdout.reconfigure(encoding="utf-8")
PY = sys.executable
HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 else os.path.dirname(HERE)

# 语料可能在几个地方（本机开发时能拿到就直接用，拿不到就跳过建索引）
CORPORA = ("pinyin.txt", "ime-base.dict.yaml", "words_alpha.txt",
           "google-20k.txt", "google-10000-english-no-swears.txt")
CANDIDATES = [
    os.path.join(REPO, "assets"),
    os.path.expanduser("~/.workbuddy/skills/west-expiring-domain-radar/assets"),
]

TMP = os.path.join(tempfile.gettempdir(), "wdr-selftest")
if os.path.exists(TMP):
    shutil.rmtree(TMP)
shutil.copytree(REPO, TMP, ignore=shutil.ignore_patterns(".git", "__pycache__",
                                                         "reports", "logs", "dev"))
shutil.copy2(os.path.join(TMP, "config.example.env"), os.path.join(TMP, "config.env"))
for d in ("reports", "logs"):
    os.makedirs(os.path.join(TMP, d), exist_ok=True)

# 造一份假报告/日志，测 report / log / status
open(os.path.join(TMP, "reports", "2026-09-25.html"), "w", encoding="utf-8").write(
    "<html><body>demo</body></html>")
open(os.path.join(TMP, "reports", "2026-09-25.md"), "w", encoding="utf-8").write(
    "# 过期域名雷达 · 2026-09-25\n\n（自测用的假报告）\n")
open(os.path.join(TMP, "logs", "2026-09-25.log"), "w", encoding="utf-8").write(
    "过期域名雷达　2026-09-25 09:00:00 CST\n── radar.py 退出码 = 0\n")

got = 0
for c in CANDIDATES:
    if all(os.path.exists(os.path.join(c, n)) for n in CORPORA):
        for n in CORPORA:
            shutil.copy2(os.path.join(c, n), os.path.join(TMP, "assets", n))
        got = 1
        break

FAKE_CRON = os.path.join(TMP, ".fake-crontab")
open(FAKE_CRON, "w", encoding="utf-8").write(
    'MAILTO=""\n*/5 * * * * /usr/bin/something-else   # 别人的任务\n')
ENV = dict(os.environ, WDRADAR_CRONTAB_FILE=FAKE_CRON, PYTHONIOENCODING="utf-8")
ENV.pop("WDRADAR_DRY_RUN", None)

FAIL = []


def run(desc, *args, expect=0, show=0, env=None):
    r = subprocess.run([PY, "manage.py", *args], cwd=TMP, env=env or ENV,
                       stdin=subprocess.DEVNULL,
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    ok = r.returncode == expect
    if not ok:
        FAIL.append("%s（期望退出码 %d，实际 %d）" % (desc, expect, r.returncode))
    print("\n%s %s  ← manage.py %s" % ("[OK]" if ok else "[!!]", desc, " ".join(args)))
    out = ((r.stdout or "") + (r.stderr or "")).strip()
    for l in (out.split("\n")[:show] if show else []):
        print("      " + l)
    return r


def cronlines():
    print("      crontab: " + " | ".join(
        l for l in open(FAKE_CRON, encoding="utf-8").read().strip().split("\n")))


print("=== 0) 路径自检（tools/ 从仓库根解析）===")
for p in ("scripts/radar.py", "scripts/lang.py", "scripts/build_assets.py",
          "tools/send_report.py", "tools/fetch_corpora.py", "manage.py", "run.sh"):
    print("      %-30s %s" % (p, "OK" if os.path.exists(os.path.join(TMP, p)) else "缺失!"))

if got:
    print("\n=== 1) 构建语言索引 ===")
    r = subprocess.run([PY, "build_assets.py"], cwd=os.path.join(TMP, "scripts"), env=ENV,
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    print("[%s] build_assets 退出码 %d" % ("OK" if r.returncode == 0 else "!!", r.returncode))
    for l in (r.stdout + r.stderr).strip().split("\n")[-4:]:
        print("      " + l)
    if r.returncode != 0:
        FAIL.append("build_assets")
    r = subprocess.run([PY, "lang.py"], cwd=os.path.join(TMP, "scripts"), env=ENV,
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    print("[%s] lang.py 自检退出码 %d" % ("OK" if r.returncode == 0 else "!!", r.returncode))
    if r.returncode != 0:
        FAIL.append("lang.py")
else:
    print("\n=== 1) 跳过建索引（本机没有语料，正式安装时由 fetch_corpora 下载）===")

print("\n=== 2) 管理面板命令 ===")
run("status（未设定）", "status", show=8)
run("time 09:30", "time", "09:30", show=2)
cronlines()
run("status（已设定，应显示 09:30）", "status", show=6)
run("pause", "pause", show=2)
cronlines()
run("status（已暂停）", "status", show=6)
run("resume", "resume", show=2)
cronlines()
run("email <地址>", "email", "you@example.com", show=2)
run("email -（清空）", "email", "-", show=2)
run("email（非交互无参数，应报错退出码 2）", "email", expect=2, show=2)
run("time 9（简写）", "time", "9", show=2)
run("time 25:00（非法，应提示且不写 cron）", "time", "25:00", show=2)
cronlines()
run("report", "report", show=6)
run("log", "log", show=4)
run("test-mail（未配 SMTP，应提示）", "test-mail", show=3)
run("未知命令（退出码 2）", "nope", expect=2, show=2)

print("\n=== 3) 卸载（dry-run，且应只删自己那行）===")
before = open(FAKE_CRON, encoding="utf-8").read()
r = subprocess.run([PY, "manage.py", "uninstall", "-y"], cwd=TMP,
                   env=dict(ENV, WDRADAR_DRY_RUN="1"), stdin=subprocess.DEVNULL,
                   capture_output=True, text=True, encoding="utf-8", errors="replace")
after = open(FAKE_CRON, encoding="utf-8").read()
print("[%s] uninstall 退出码 %d" % ("OK" if r.returncode == 0 else "!!", r.returncode))
print("      卸载后 crontab: %r" % after.strip())
if "something-else" not in after:
    FAIL.append("卸载把别人的 cron 任务也删了")
else:
    print("      ✓ 别人的 cron 任务未被误删")
if "west-radar-daily" in after:
    FAIL.append("卸载后仍残留我们的 cron 行")
else:
    print("      ✓ 我们的 cron 行已移除")

print("\n=== 4) 交互菜单渲染（喂 q）===")
r = subprocess.run([PY, "manage.py"], cwd=TMP, env=ENV, input="q\n",
                   capture_output=True, text=True, encoding="utf-8", errors="replace")
print("[%s] 菜单退出码 %d" % ("OK" if r.returncode == 0 else "!!", r.returncode))
print(r.stdout)

print("\n" + "=" * 62)
if FAIL:
    print("失败 %d 项：" % len(FAIL))
    for f in FAIL:
        print("  - " + f)
    sys.exit(1)
print("全部通过 ✓")
