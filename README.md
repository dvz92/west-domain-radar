# 过期域名雷达

在 VPS 上每天自动抓取 [west.cn](https://www.west.cn/booking/) 的过期域名预订池，按**你的选品口味**排序，
输出 HTML 报告并邮件发送。带一个交互式管理面板。

```
┌──────────────────────────────────────────────────────────────┐
│        过期域名雷达 · 管理面板                                 │
└──────────────────────────────────────────────────────────────┘
   状态   运行中 每天 09:00（北京时间）
   推送   wecom、email
   报告   2026-09-25.html
──────────────────────────────────────────────────────────────
   d) 立即运行一次       马上抓取并出报告（不等定时）
   n) 设定推送方式       企业微信 / 钉钉 / 飞书 / 邮件 / Telegram …
   e) 设定通知邮箱       邮件渠道的收件地址
   t) 设定每天启动时间   北京时间几点跑
   ·────────────────────────────────────────────────────────
   m) 发送测试推送       验证推送渠道是否配通
   r) 查看最近报告       路径 + 摘要
   l) 查看最近日志       排查失败原因
   p) 暂停 / 启用定时    临时停跑，配置保留
   u) 完整卸载           删定时任务 + 短命令 + 整个目录
──────────────────────────────────────────────────────────────
```

## 安装（两行）

```bash
git clone https://github.com/dvz92/west-domain-radar.git
cd west-domain-radar && bash install.sh
```

安装脚本会把文件复制到 `~/west-radar`，所以**克隆目录可以随便放**（放 `/tmp` 都行）。
也可以直接克隆到安装目录里就地安装，脚本会自动跳过复制：

```bash
git clone https://github.com/dvz92/west-domain-radar.git ~/west-radar
cd ~/west-radar && bash install.sh
```

不想 clone，也可以直接远程跑：

```bash
curl -fsSL https://raw.githubusercontent.com/dvz92/west-domain-radar/main/install.sh \
  | REPO_SLUG=dvz92/west-domain-radar bash
```

想一步把推送也配好（以企业微信机器人为例）：

```bash
curl -fsSL https://raw.githubusercontent.com/dvz92/west-domain-radar/main/install.sh \
  | REPO_SLUG=dvz92/west-domain-radar WECOM_WEBHOOK='https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxx' bash
```

装完会：装 python3（缺的话）→ 下载语料并建索引 → 自检模型 → 装定时任务 → **立刻试跑一次**。

## 更新到最新版

```bash
cd ~/west-domain-radar && bash install.sh      # 就地安装过的话
# 或者：在克隆目录里 git pull && bash install.sh
```

`install.sh` **幂等**：重复运行只会覆盖程序文件，不会动 `config.env`、`reports/`、`logs/`，
也不会把定时任务加成两条。

## 装完怎么用

```bash
wdradar                       # 打开管理面板（推荐）
wdradar run                   # 不等菜单，直接跑一次
wdradar status                # 看状态
wdradar notify                # 设定推送方式
wdradar channels              # 看已启用的渠道 / 每个渠道还缺什么
wdradar test                  # 发一条测试推送
wdradar email you@qq.com      # 设定邮件收件人
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

### 推送：不止邮件（推荐先配企业微信机器人）

海外 VPS 上**邮件经常发不出去**，三个典型原因：

| 现象 | 原因 |
| --- | --- |
| `Connection refused` / 超时 | 机房封了出站 25 / 465 / 587 端口 |
| `535 Authentication failed` | 授权码错（或用了登录密码） |
| `554` / 被退信 | 收件方把 VPS 的境外 IP 当垃圾邮件来源拒了 |

而「机器人 Webhook」就是一次 HTTPS POST，**不碰邮件端口、不涉及收件方风控**，最稳。

| 渠道 | 配置键 | 特点 |
| --- | --- | --- |
| **企业微信机器人** ⭐ | `WECOM_WEBHOOK` | 最省事最稳。群里建个机器人，复制 Webhook 就行，手机上直接看 |
| 钉钉机器人 | `DINGTALK_WEBHOOK`（+`DINGTALK_SECRET` 加签） | 同上 |
| 飞书机器人 | `FEISHU_WEBHOOK` | 同上 |
| Server酱 / PushPlus | `SERVERCHAN_KEY` / `PUSHPLUS_TOKEN` | 推到**个人微信**，扫码即得 |
| Telegram Bot | `TG_BOT_TOKEN` + `TG_CHAT_ID` | 海外 VPS 极稳，但手机端在国内要能连 Telegram |
| ntfy | `NTFY_TOPIC` | 手机装 App 订阅一个 topic，**不用注册**，还能自建 |
| 自定义 Webhook | `CUSTOM_WEBHOOK` | POST JSON：`title` / `content` / `source` / `date` |
| 邮件 | `SMTP_*` + `MAIL_TO` | 保留，建议只当兜底 |

**多个渠道会自动失败转移**：按 `NOTIFY_CHANNELS` 的顺序依次尝试，**第一个成功就停**。

```ini
NOTIFY_CHANNELS=wecom,email     # 企业微信优先，邮件兜底
```

> ⚠️ 只把渠道名写进 `NOTIFY_CHANNELS` **不算配好** —— 参数没填全的渠道会被自动跳过，
> 状态里也不会显示成"已启用"。用 `wdradar channels` 能看到每个渠道还缺什么。

#### 企业微信机器人（三步）

1. 手机/电脑上建一个只有自己的企业微信群（或直接用现成的群）
2. 群右上角 **「…」→ 群机器人 → 添加机器人 → 新创建**，起个名
3. 复制它给的 **Webhook 地址**（形如 `https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxxx`）

然后 VPS 上：

```bash
wdradar            # 选 n → 选 1 → 粘贴 Webhook → 选 y 发测试
```

#### 报告文件怎么办

推送渠道只能发**文字消息**，所以正文里带的是 Top10 摘要；**完整 HTML 报告留在 VPS 上**：

```bash
wdradar report                                 # 看路径和摘要
scp user@你的VPS:~/west-radar/reports/2026-09-26.html .
```

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

#### QQ 邮箱：怎么拿授权码

QQ 邮箱**默认关闭**第三方客户端的收发服务，所以要手动开启并生成一串专用密码。

1. 浏览器登录 **mail.qq.com**
2. 右上角 **「设置」→「账号与安全」**（旧版界面叫「设置」→「账户」）
3. 找到 **「POP3/IMAP/SMTP/Exchange/CardDAV/CalDAV服务」** 区域 → 点 **「开启」**
   （开 **IMAP/SMTP 服务** 就够本程序发信用）
4. 按页面提示，用你**绑定的手机号发送指定内容到指定号码**，发完点「我已发送」
5. 验证通过后会弹出一串 **16 位纯字母**的授权码，形如 `abcdabcdabcdabcd`
6. ⚠️ **只显示这一次，关掉页面就看不到了**，先复制保存好

忘了或想重置：**「设置」→「账号与安全」→「设备管理」→「授权码管理」**
（手机端：设置 → 点对应账号 → 安全管理 → 设备管理）。

拿到后对照着填：

```ini
MAIL_TO=          # 你想收报告的邮箱（可以是另一个邮箱）
SMTP_HOST=smtp.qq.com
SMTP_PORT=465
SMTP_SECURITY=ssl
SMTP_USER=123456@qq.com     # 完整邮箱地址，不要只写 QQ 号
SMTP_PASS=abcdabcdabcdabcd  # ← 就是这串 16 位授权码
```

装好之后不用手改文件，直接进面板按 **`s`** 一路填完，再按 **`m`** 发封测试邮件验证。

**两个常见坑**

- **改了 QQ 密码 → 授权码立即失效**，要重新生成再用。
- 首次发信可能被对方判为垃圾邮件：去垃圾箱点一次「不是垃圾邮件」，
  或者干脆把 `MAIL_TO` 设成**发件邮箱自己**，先跑通再加别的收件人。

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

### ⚠️ GitHub 不接受账号密码了（2021-08-13 起）

`git push` 提示 `Username for 'https://github.com':` / `Password:` 时，**密码填什么都不对**。
三条路，从省事到麻烦：

**① 仓库设成 public —— 最省事**

公开仓库在 VPS 上 `git clone` **完全不需要认证**。这个项目里没有敏感信息（配置在
`config.env`，已被 `.gitignore` 排除），所以公开是最省心的选择。

**② SSH key —— 一次配置，以后免密**

```bash
ssh-keygen -t ed25519 -C "你的邮箱"      # 一路回车，密码可留空
cat ~/.ssh/id_ed25519.pub                # 复制输出的整行
```

粘贴到 GitHub → Settings → **SSH and GPG keys** → New SSH key，然后换 remote：

```bash
git remote set-url origin git@github.com:<你>/west-domain-radar.git
git push -u origin main
```

**③ Personal Access Token —— 当密码用**

GitHub → 头像 → Settings → Developer settings → Personal access tokens →
**Fine-grained tokens**（或 Tokens classic，勾 `repo`）→ 生成后**只显示一次**，先复制走。

push 时：用户名填 **GitHub 用户名**，密码处**粘贴 token**。

嫌每次都要输，可以缓存（注意 `store` 是明文存在 `~/.git-credentials`）：

```bash
git config --global credential.helper store     # 永久（明文）
git config --global credential.helper cache     # 或：15 分钟内有效
```

### 私有仓库在 VPS 上怎么 clone

不要用 `https://用户名:token@github.com/...`（token 会明文留在 `.git/config`）。
推荐二选一：

- **Deploy key（只读，最干净）**：仓库 → Settings → Deploy keys → Add deploy key，
  把上面 `id_ed25519.pub` 的内容贴进去，然后
  `git clone git@github.com:<你>/west-domain-radar.git`
- **`gh auth login`（完全不碰密码）**：装 GitHub CLI 后运行，走设备码授权，
  终端里只输一次性验证码，不涉及账号密码

### 终端里粘贴密码

在 Tabby 里默认是 **`Ctrl+Shift+V`**（macOS `⌘+V`）；也可以右键 → Paste
（右键行为可在 设置 → 终端 里改为直接粘贴）。

**密码提示下屏幕不会有任何回显**（连星号都没有），这是正常的安全行为，粘贴完直接回车。

> 如果粘贴长 token 后报认证失败，去 设置 → 终端 把 **括号粘贴模式（bracketed paste）关掉**
> 再试 —— 某些提示下终端会额外发送 `ESC[200~` / `ESC[201~` 包裹序列，被当成凭证的一部分。

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
