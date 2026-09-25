# 过期域名雷达

在 VPS 上每天自动抓取 [west.cn](https://www.west.cn/booking/) 的过期域名预订池，按**你的选品口味**排序，
输出 HTML 报告并邮件发送。带一个交互式管理面板。

```
┌──────────────────────────────────────────────────────────────┐
│        过期域名雷达 · 管理面板                                 │
└──────────────────────────────────────────────────────────────┘
   状态   运行中 每天 09:00（北京时间）
   邮箱   you@qq.com
   报告   2026-09-25.html
──────────────────────────────────────────────────────────────
   d) 立即运行一次       马上抓取并出报告（不等定时）
   e) 设定通知邮箱       报告发到哪个邮箱
   s) 设定发件服务器     SMTP（授权码）；配错就收不到信
   t) 设定每天启动时间   北京时间几点跑
   ·────────────────────────────────────────────────────────
   m) 发送测试邮件       验证邮箱配置是否正确
   r) 查看最近报告       路径 + 摘要
   l) 查看最近日志       排查失败原因
   p) 暂停 / 启用定时    临时停跑，配置保留
   u) 完整卸载           删定时任务 + 短命令 + 整个目录
──────────────────────────────────────────────────────────────
```

## 安装（两行）

```bash
git clone https://github.com/<你>/west-domain-radar.git
cd west-domain-radar && bash install.sh
```

不想 clone，也可以直接远程跑（把 `<你>` 换成你的 GitHub 用户名）：

```bash
curl -fsSL https://raw.githubusercontent.com/<你>/west-domain-radar/main/install.sh \
  | REPO_SLUG=<你>/west-domain-radar bash
```

想一步把邮件也配好：

```bash
curl -fsSL https://raw.githubusercontent.com/<你>/west-domain-radar/main/install.sh \
  | REPO_SLUG=<你>/west-domain-radar \
    MAIL_TO=you@qq.com SMTP_HOST=smtp.qq.com SMTP_USER=you@qq.com SMTP_PASS=授权码 bash
```

装完会：装 python3（缺的话）→ 下载语料并建索引 → 自检模型 → 装定时任务 → **立刻试跑一次**。

## 装完怎么用

```bash
wdradar                       # 打开管理面板（推荐）
wdradar run                   # 不等菜单，直接跑一次
wdradar status                # 看状态
wdradar email you@qq.com      # 设定收件邮箱
wdradar time 09:30            # 设定每天北京时间 9:30 跑
wdradar pause / resume        # 暂停 / 启用定时
wdradar uninstall             # 完整卸载
```

`wdradar` 装在 `/usr/local/bin` 或 `~/.local/bin`。装不上就直接
`python3 ~/west-radar/manage.py`。

## 工作原理

### 数据源

**只从 west.cn 取数**（`/services/grabnew/newlist.asp`）。它免登录、支持按长度/后缀/删除日期/排序筛选，
是唯一同时覆盖 `.com/.cn/.top` 的源。

> 试过 22.cn（爱名网）：接口确实更好用（单次 200 条、自带域名分类），但**它的池子不是 west.cn 的超集**
> ——实测它查不到 west.cn 池里真实存在的域名。换出口可以，**换站点不行**，所以没有采用。

候选口径：

| 后缀 | 范围 |
| --- | --- |
| `.com` | 纯字母 5–8 位（1–4 位必进竞拍，散户拿不到，所以下限设在 5 位） |
| `.cn`  | 纯字母 ≤8 位，只要一级 cn |
| `.top` | 纯字母 ≤8 位 |

### 选品模型

按你的口味分四层，不沾边的直接淘汰：

| 层 | 类别 | 怎么判 |
| --- | --- | --- |
| C1 | **英文单词** | 在英文词频表内（有实义） |
| C2 | **可发音英文** | 音位结构合法 + **品牌感评分**（4–6 位） |
| C3 | **双拼 / 三拼** | 能完整拆成合法拼音音节；是真实词语再加分 |
| C4 | **拼音首字母** | 4 位字母能对上**人工好词表**里的常用四字词（如 `xrmm` → 笑容满面） |
| — | 其他 | 拆不出拼音、读不出、首字母对不上 → **淘汰**（`rpkc`、`jxggw`、`dmon`） |

其中「品牌感」（`scripts/lang.py` 的 `brandability()`）是关键，它是按一组**真人挑出来的样例**校准的：

- 正样本：`sioly` `rofar` `kaote` `atiron` `korp` `fary` `mexa` `rady` `kombio` `sery` `glax`
- 它测的是：元音组数、词尾是否开音节/响音、辅元交替度、有没有生硬的辅音簇
- 原来只用"字母三元组频率"打分，结果是**垃圾分比正样本高**（`ised` 82.0 > `atiron` 81.3），
  因为高频片段（`mon`/`ed`/`is`）会让毫无品牌感的串得分虚高

想调口味：
- `assets/good4.txt` —— 拼音首字母的**白名单**，直接加你认可的四字词
- `assets/watchlist.txt` —— 你点名的域名，每天会单独核查状态，**并并入候选池参与排序**
  （com 池 1.6 万条只能抽样，靠它保证不漏）

### 邮件

`smtplib` 直发，附件是 HTML + Markdown，正文是 Top10 摘要 + 费用提示 + 定向核查。

| 邮箱 | SMTP_HOST | 端口 | 加密 |
| --- | --- | --- | --- |
| QQ | smtp.qq.com | 465 | ssl |
| 163 | smtp.163.com | 465 | ssl |
| Gmail | smtp.gmail.com | 465 | ssl |
| Outlook | smtp.office365.com | 587 | starttls |

⚠️ **必须用「授权码 / 应用专用密码」，不是登录密码。**
⚠️ 海外 VPS **别用 25 端口**（多数机房封），用 465 或 587。

## 关于 west.cn 的访问限制（重要）

- 匿名访问硬限制：**单次最多 50 条**、**第 2 页起要登录**、**请求过快会封 IP**（并发 4 个即可触发，封 1 小时以上）。
- 所以程序是**严格串行 + 每次间隔 2.5 秒 + 单次约 30 个请求**。
- **一天只跑一次就够。**当天重复全量跑，两次叠加就会触发限流。
- 被限流时程序返回退出码 3：**不写空报告、不编造域名**，改发一封说明邮件。
- 想彻底解决要登录 west.cn（金卡单次可查 5000 条、可分页），本项目不含登录逻辑。

## 目录结构

```
west-domain-radar/
├── install.sh              一键安装（支持本地仓库 / 远程下载两种模式）
├── manage.py               交互式管理面板
├── run.sh                  每日运行入口（cron 调它）
├── config.example.env      配置模板
├── dev/selftest.py         自测（不联网、不发信、不碰真实 crontab）
├── scripts/
│   ├── radar.py            抓取 + 打分 + 出报告
│   ├── lang.py             语言分类 + 品牌感评分
│   └── build_assets.py     从语料生成运行时索引
├── tools/
│   ├── fetch_corpora.py    下载公开语料
│   └── send_report.py      SMTP 发信
└── assets/
    ├── good4.txt           拼音首字母白名单（人工维护）
    └── watchlist.txt       定向核查清单（人工维护）
```

运行时还会产生 `reports/`、`logs/`、`state.json`、`config.env`，都不进版本库（见 `.gitignore`）。
语料与生成的索引也在 `.gitignore` 里 —— 安装时自动下载/构建。

改完代码想验一下，跑自测（不需要 VPS、不需要联网）：

```bash
python3 dev/selftest.py
```

## 推到自己的仓库

```bash
gh repo create west-domain-radar --public --source=. --push    # 有 gh 的话
# 或者手动：
git remote add origin https://github.com/<你>/west-domain-radar.git
git branch -M main && git push -u origin main
```

## 系统要求

- Linux（Debian / Ubuntu / CentOS / Alpine 都行），或任何能跑 python3 的机器
- **python3 ≥ 3.8，不需要 pip 装任何第三方包**（全部标准库）
- 约 60MB 磁盘（语料 22MB + 索引 9MB + 报告）
- crontab（装了才能定时；`NO_CRON=1` 可跳过）

## 迁移 / 卸载

```bash
# 换机器：整个目录拷过去就行，语料索引都在里面
tar czf west-radar.tar.gz -C ~ west-radar

# 卸载
wdradar uninstall
```

## License

MIT
