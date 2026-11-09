# workbuddy_auto_checkin

WorkBuddy「Buddy 加油站」的每日自动化：**签到领积分**、**派猫猫旅行并领回礼物**，结果推送到**微信**（企业微信群机器人 / Server酱 / PushPlus，三选一或全开），并**在访问令牌临近过期时提前提醒**。

跑在 GitHub Actions 上，按 cron 每天定时执行，不需要任何常开设备。

> **两种运行方式，签到逻辑完全一致，二选一：**
>
> | | GitHub Actions 版（本 README） | [WPS 金山文档版](wps/README.md) |
> | --- | --- | --- |
> | 适合 | 习惯 GitHub 的用户 | **不想碰 GitHub / 零命令行** 的用户 |
> | 托管 | GitHub Actions 免费定时任务 | 金山文档智能表格脚本 + 免费定时任务 |
> | 配置 | Fork 仓库 + 填 Secret | 建表格 → 粘脚本 → 填单元格 |
> | 结果 | Actions 日志 / 微信消息 | 直接回写表格 |
>
> 两种版本的 token 都由本机导出一次（[第 4 步](#第-4-步导出登录凭证二选一)）。
> 本机导出脚本在 **Windows 与 macOS** 上均可直接运行（已适配两端的路径探测）。

## 特性

- **云端定时** — 免费 runner + `schedule` 触发，本机关机照常执行
- **一次做两件事** — 签到与成长中心旅行在同一次运行里完成
- **幂等** — 先查状态再决定是否领取；已签不重领，已派不重派
- **结论解耦** — 猫猫模块任何异常都不影响签到结论；整体成败只看签到
- **令牌到期预警** — 每次运行顺带检查 token 剩余有效期，剩 7 天转黄、3 天转红，直接写进通知
- **零明文入库** — 仓库不含任何凭据；token 由本机脚本导出后写入 GitHub Secret
- **推送到微信** — 企业微信群机器人 / Server酱 / PushPlus 三条通道任选，填了哪条推哪条

## 工作原理

```text
本机（一次性）                      GitHub                        外部
┌──────────────────┐   gh secret   ┌─────────────────────┐
│ refresh_token.py │ ─────────────► │ Secret:             │
│  读登录态 → 解密 │                │  WB_ACCESS_TOKEN    │
└──────────────────┘                │  WECOM_WEBHOOK      │
                                    │  /SERVERCHAN_SENDKEY│
                                    │  /PUSHPLUS_TOKEN    │
                                    └─────────┬───────────┘
                                              │ 每天 cron
                                    ┌─────────▼──────────┐        ┌──────────────┐
                                    │ Actions: wb_auto.py│ ─────► │ copilot.     │
                                    │  签到/领奖/派出     │ ◄───── │ tencent.com  │
                                    │  令牌到期检查       │        └──────────────┘
                                    └─────────┬──────────┘
                                              │ 推送
                                    ┌─────────▼──────────┐
                                    │ 企业微信 / 个人微信 │
                                    └────────────────────┘
```

### 为什么 token 必须在本机导出

桌面端把会话写在 `workbuddy-desktop.info` 里，其中 `accessToken` 是 Electron `safeStorage` 保护的 `$wbEncrypted` 信封（AES-256-GCM，密钥由客户端运行时持有）。云端 runner 既没有这份文件，也没有解开它所需的运行时，因此**无法在云端现取 token**。

`refresh_token.py` 的做法是：读出加密信封 → 以 `ELECTRON_RUN_AS_NODE=1` 起一个 WorkBuddy 子进程 → 把信封通过 stdin 递进去，由客户端自己的运行时解密 → 从 stdout 取回明文 token。解密材料全程不落盘。

## 快速开始

### 开始前要有

- 一个 GitHub 账号
- 一台装过 WorkBuddy **并且登录过**的电脑（只在配置时用一次）
- **一个接收消息的微信通道**（三选一，见 [第 3 步](#第-3 步接一个微信推送通道三选一)）
- 电脑上有 Python 3.8+（Windows 用户没有也行——可用仓库里的 `refresh_token.exe`，见第 4 步）

### 容易搞混的一点，先说清楚

下面所有命令和配置里出现的仓库名，指的都是**你自己**的那个仓库（`你的用户名/workbuddy_auto_checkin`），**不是**原作者的 `FXmonkey/workbuddy_auto_checkin`。原作者的仓库只是让你 Fork 的源头，你的凭证和定时任务都配在你自己的仓库里。

### 第 1 步：把项目复制到自己账号下

两种方式都行，**推荐 Fork**，一键搞定：

**方式 A：Fork（推荐）**

1. 打开项目页 `https://github.com/FXmonkey/workbuddy_auto_checkin`
2. 点右上角 **Fork** → **Create fork**
3. 完成后你就有了 `你的用户名/workbuddy_auto_checkin`
4. **fork 完必须多做一步**：进你 fork 出来的仓库 → 点上方 **Actions** 页签 → 页面中间会有一行提示，点 **I understand my workflows, go ahead and enable them** 启用。**不启用的话定时任务不会跑。**

**方式 B：新建仓库再上传**

1. GitHub 右上角 **+** → **New repository**，名字随便填，选 **Private**，点 **Create repository**
2. 仓库页点 **Add file → Upload files**
3. 把整个项目文件夹拖进去（**必须包含 `.github` 这个文件夹**，它是定时任务的定义），点 **Commit changes**

> 两种方式的取舍：Fork 少一步，但要多做上面第 4 小步的启用；新建仓库没有这个坑，但网页上传时容易漏掉 `.github` 这种以点开头的文件夹。二选一即可。

### 第 2 步：把脚本下载到自己电脑

后面要在你电脑上跑一次脚本导凭证，所以先把代码拿到本地。两种都行：

- 网页下载：你的仓库页 → 绿色 **Code** 按钮 → **Download ZIP** → 解压
- 或用 git：`git clone https://github.com/FXmonkey/workbuddy_auto_checkin.git`

### 第 3 步：接一个微信推送通道（三选一）

三条通道**互相独立**，填哪条就推哪条，也可以同时填多条一起推。按你手上的条件挑一条即可：

| 通道 | 适合谁 | 你需要的 | 对应 Secret |
| --- | --- | --- | --- |
| **企业微信群机器人** | 有企业微信 | 群机器人 webhook 地址 | `WECOM_WEBHOOK` |
| **Server酱** | 只有个人微信 | SendKey（`SCT` 开头） | `SERVERCHAN_SENDKEY` |
| **PushPlus** | 只有个人微信 | token | `PUSHPLUS_TOKEN` |

**方案 1：企业微信群机器人（若你所在组织能用企业微信，最稳）**

1. 企业微信里建一个**内部群**（只有自己也行；若客户端要求至少 3 人，先拉人建群再把其他人移出）→ 群设置 → **群机器人**（新版界面可能叫 **消息推送**）→ **添加机器人**
2. 复制它给出的 Webhook 地址，形如
   `https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxxxxxxx`
3. **不要勾选「加签 / 签名校验」** —— 脚本发送的是未签名请求，开启后推送会被拒

> 个人可免费注册企业微信，群机器人 Webhook **不需要企业认证**；限流 20 条/分钟、无每日条数上限。

**方案 2：Server酱（推送到个人微信）**

1. 打开 `https://sct.ftqq.com/`，用微信扫码登录
2. 在「SendKey」页复制你的 SendKey（形如 `SCTxxxxxxxx`），并按其指引关注服务号完成绑定

**方案 3：PushPlus（推送到个人微信）**

1. 打开 `https://www.pushplus.plus/`，微信扫码登录
2. 首页复制你的 `token`

> 三条通道都是纯文本消息，签到结论与令牌到期提醒会一起发过去。

> ⚠️ WPS 金山文档版不使用以上通道，它直接回写表格，见 [wps/README.md](wps/README.md)。

### 第 4 步：导出登录凭证（二选一）

**先选运行形式：**

- **没装 Python（仅 Windows）？** 直接双击仓库里的 **`refresh_token.exe`**（用法与 `.py` 完全一致，把下面命令里的 `python refresh_token.py` 换成 `refresh_token.exe` 即可）。首次运行 Windows SmartScreen 可能拦截，点 **更多信息 → 仍要运行**。
- **装了 Python（Windows / macOS 均可）**：继续往下按命令操作（脚本零第三方依赖，只需 Python 3.8+）。

**方式 A：电脑上装了 gh CLI —— 自动写入，最省事**

在项目文件夹里运行：

```
python refresh_token.py --gh-repo 你的用户名/workbuddy_auto_checkin
```

脚本会解密并直接把 token 写进你仓库的 Secret，屏幕显示「已写入」即可跳到第 5 步。

> 前提是 `gh` **已登录**（`gh auth status` 能看到账号）。仅安装而未登录时会写入失败，此时改用下方方式 B。

**方式 B：没装 gh CLI —— 先打印出来，再手动粘贴**

在项目文件夹里运行：

```
python refresh_token.py
```

屏幕上会打印一长串字符（下图涂黑的部分就是它），**完整复制 `----- 以下为 token -----` 到 `----- 以上为 token -----` 之间**的内容，下一步手动粘进 GitHub。脚本末尾还会打印 **[到期时间]**，记下这个日期，接近时再跑一次即可。

> **macOS 用户**：凭据文件通常在
> `~/Library/Application Support/CodeBuddyExtension/Data/Public/auth/workbuddy-desktop.info`，
> 脚本已内置该路径，直接运行即可；若提示未找到，用
> `WORKBUDDY_AUTH_FILE` / `WORKBUDDY_EXE` 指定后重试。

![](assets/refresh_token输出.jpg)



### 第 5 步：把 Secret 填进仓库

打开**你自己的仓库** → **Settings** → 左侧 **Secrets and variables** → **Actions** → 点绿色的 **New repository secret**：

![](assets/github_配置action的飞书、wb_access_token.png)

需要填的 Secret：

| Name | 必填 | Secret 里填什么 |
| --- | --- | --- |
| `WB_ACCESS_TOKEN` | **是** | 第 4 步方式 B 复制的那串字符 |
| `WECOM_WEBHOOK` | 三选一 | 第 3 步方案 1 的企业微信机器人地址 |
| `SERVERCHAN_SENDKEY` | 三选一 | 第 3 步方案 2 的 Server酱 SendKey |
| `PUSHPLUS_TOKEN` | 三选一 | 第 3 步方案 3 的 PushPlus token |

> 方式 A 已经自动写入 `WB_ACCESS_TOKEN` 的，这里只需再填**一条**推送通道的 Secret。
> 推送通道的 Secret 至少要填一个，否则脚本只打日志、不推送。
> 填完后这个页面应该能看到相应数量的记录（如上面截图所示）。

### 第 6 步：手动跑一次验证

1. 打开你的仓库 → 点上方 **Actions** 页签
2. 左侧选 **Buddy 加油站每日自动化**
3. 右侧点 **Run workflow** 下拉，再点绿色的 **Run workflow**
4. 等十几秒，刷新页面，微信应该收到一条消息

> 这个页面显示绿色不代表签到一定成功（猫猫出问题不影响整体结论）。**以微信收到的消息为准。**

## 令牌到期提醒

访问令牌由客户端签发，**有效期 30 天**（凭据文件 `expiresIn` = `2592000` 秒，`refreshToken` 有效期 60 天）。token 是标准 JWT，脚本在每次运行时**本地解析其 `exp` 声明**得知剩余天数，无需额外配置任何 Secret，也不会把 token 发去任何第三方。

提醒分四档，直接写进每次推送的消息里：

| 剩余天数 | 提示 |
| --- | --- |
| > 7 天 | ✅ 访问令牌有效期至 YYYY-MM-DD（剩 N 天） |
| ≤ 7 天 | 🟡 …剩 N 天，建议近日重跑 `refresh_token.py` 重新注入 |
| ≤ 3 天 | 🔴 …仅剩 N 天，请尽快重跑 `refresh_token.py` 重新注入 |
| 已过期 | 🔴 访问令牌已过期，签到会失败，请立即重跑 `refresh_token.py` |

> 因此**你不需要自己记日子**：只要 cron 在跑，临近到期时会连续多天收到黄色/红色提醒。
> 收到提醒后按 [令牌续期](#令牌续期) 的三步操作即可。

## 令牌续期

> 访问令牌有效期 30 天。收到 🟡 / 🔴 到期提醒后按本节操作，全程约 3 分钟。
> **不需要改代码、不需要重新部署、不需要重启 workflow**；更新后的 Secret 在下一次运行自动生效。

### 什么时候做

| 推送里的提示 | 建议动作 |
| --- | --- |
| 🟡 剩 ≤ 7 天 | 任意一天完成续期（推荐，留足余量） |
| 🔴 剩 ≤ 3 天 | 尽快完成 |
| 已过期 / 日志出现「认证被拒」 | 立即完成 |

### 第 1 步：在本机导出新令牌

1. **先打开 WorkBuddy 客户端**并确认处于登录状态。客户端会自动换发新的 `accessToken`
   并写回本机凭据文件；不先打开，可能导出到上一枚旧令牌。
2. 在项目目录运行（macOS 用 `python3`；Windows 可用 `python` 或双击 `refresh_token.exe`）：

   ```bash
   cd ~/workbuddy_auto_checkin   # 换成你自己的克隆目录
   python3 refresh_token.py
   ```

3. 输出结构如下（token 本体略去）：

   ```text
   凭据为加密信封，正在通过本机客户端解密…
   ----- 以下为 token -----
   eyJhbGciOiJSUzI1...（约 1500 字符，一整行）
   ----- 以上为 token -----
   [到期时间] YYYY-MM-DD HH:MM（剩余约 30.0 天）
   ```

4. **完整复制两个标记之间的整行**，不含标记、空格或换行。
5. **自检**：`[到期时间]` 必须是「当天 + 约 30 天」。若仍是旧日期，说明客户端尚未换发，
   退出并重开客户端后重试。

### 第 2 步：在 GitHub 网页覆盖 Secret

1. 打开 `https://github.com/OWNER/REPO/settings/secrets/actions`
   （`OWNER/REPO` 换成你自己的仓库；也可走 仓库 → **Settings** → **Secrets and variables**
   → **Actions** → **Secrets** 标签）
2. 在列表找到 `WB_ACCESS_TOKEN`，点右侧 **Update**（编辑按钮）
3. 在 **Value** 框**整体粘贴**新令牌 → 点 **Update secret**

注意：

- GitHub **不显示已有 Secret 的值**，只能整体覆盖，这是正常设计
- **Name 保持 `WB_ACCESS_TOKEN` 不变**，不要新建、不要改名
- **不要动推送通道的 Secret**（`WECOM_WEBHOOK` / `SERVERCHAN_SENDKEY` / `PUSHPLUS_TOKEN`）
- 令牌等同登录态，请勿粘贴到聊天、截图、日志或公开仓库

### 第 3 步：验证

1. **Actions** → **Buddy 加油站每日自动化** → **Run workflow** → 绿色 **Run workflow**
2. 日志中 `[令牌]` 行显示**新的到期日**、`overall_ok: true`，且微信收到消息 → 续期完成
3. 若日志出现「认证被拒」，回到第 1 步重新导出一枚最新的令牌再覆盖一次

### 两个边界情况

- **每打开一次客户端，凭据文件里的令牌都可能被换发一次**。因此「导出 → 立刻覆盖 Secret」
  建议一次做完，中间不要搁置太久。
- **`refreshToken` 有效期 60 天**。约两个月后若脚本报「凭据文件缺少 accessToken」，
  或 `[到期时间]` 不再前进，需要在 WorkBuddy 客户端**重新登录一次**，再按本节操作。

### 可选：用命令行一步写入

若本机 `gh` CLI **已登录**（`gh auth status` 能显示账号），可直接：

```bash
python3 refresh_token.py --gh-repo OWNER/REPO
```

Windows 用户对应 `refresh_token.exe --gh-repo OWNER/REPO`。

> ⚠️ **`gh` 装了 ≠ 已登录**。脚本只检查 `gh` 命令是否存在，未登录时会写入失败并报错；
> 此时改用上面的网页方式，或先执行 `gh auth login` 完成登录。

## 配置项

### Actions 侧（`wb_auto.py`）

| 变量 | 必填 | 默认 | 说明 |
| --- | --- | --- | --- |
| `WB_ACCESS_TOKEN` | 是 | — | 访问令牌。缺失则直接退出（exit 2） |
| `WECOM_WEBHOOK` | 三选一 | — | 企业微信群机器人 webhook。填了即启用该通道 |
| `SERVERCHAN_SENDKEY` | 三选一 | — | Server酱 SendKey（`SCT` 开头）。填了即启用该通道 |
| `PUSHPLUS_TOKEN` | 三选一 | — | PushPlus token。填了即启用该通道 |
| `WB_ENDPOINT` | 否 | `https://copilot.tencent.com` | 接口地址 |
| `WB_RESULT_FILE` | 否 | — | 写入 Markdown 汇总表，供 Actions 的 step summary 使用 |

> 三条推送通道互相独立，**填了几条就推几条**；一条都没填则只打印日志、不推送。

### 本机侧（`refresh_token.py`）

| 变量 | 说明 |
| --- | --- |
| `WORKBUDDY_AUTH_FILE` | 覆盖凭据文件路径。用于非默认安装或凭据位置变动 |
| `WORKBUDDY_EXE` | 覆盖客户端可执行文件路径。自动探测失败时使用 |

**凭据文件**按以下顺序探测（Windows / macOS 均已适配）：

```text
Windows: %LOCALAPPDATA%\CodeBuddyExtension\Data\Public\auth\workbuddy-desktop.info
macOS:   ~/Library/Application Support/CodeBuddyExtension/Data/Public/auth/workbuddy-desktop.info
```

**客户端可执行文件**按 `WORKBUDDY_EXE` → 常见安装目录 → 注册表 `DisplayIcon`（Windows）→
`/Applications/WorkBuddy.app`（macOS，读 `Info.plist` 的 `CFBundleExecutable`）的顺序探测。

## 行为矩阵

| 情况 | 行为 | `result` |
| --- | --- | --- |
| 今天未签到 | 调用领取接口 | `CLAIMED` |
| 今天已签到 | 不重复领取，读状态汇报 | `ALREADY` |
| 活动未开启 | 直接汇报，不做写操作 | `INACTIVE` |
| 猫猫已到家 | 先领这趟的旅行积分，再判断派出 | `DEPARTED` / `LIMIT_REACHED` |
| 猫猫在途中 | 汇报位置与预计到家时间，不派出 | `IN_TRIP` |
| 猫猫空闲且名额未用完 | 派出一趟 | `DEPARTED` |
| 今日名额已用完 | 读服务端 `daily_limit_reached`，不派出 | `LIMIT_REACHED` |
| 认证被拒（401/403） | 提示重新运行 `refresh_token.py` | `AUTH` |
| 网络不可达 | 有限重试后汇报，不误报成功 | `NETWORK` |
| 猫猫模块异常 | 捕获后单独汇报，不影响签到 | `ERROR` |
| 旅行状态取值未知 | 原样汇报，不做写操作 | `UNKNOWN_STATE` |
| 领取返回无法识别 | 汇报原始返回，不臆断成功 | `UNKNOWN` |
| 令牌剩余 ≤ 7 天 | 在推送正文追加黄色提醒 | — |
| 令牌剩余 ≤ 3 天 / 已过期 | 在推送正文追加红色提醒 | — |

派出名额的判断读服务端下发的 `daily_limit_reached`，而不是硬撞 `depart` 接口等它报错。

## 退出码

| 码 | 含义 |
| --- | --- |
| `0` | 签到成功 |
| `1` | 签到未达成（猫猫失败不在此列） |
| `2` | 缺少 `WB_ACCESS_TOKEN` |

workflow 中脚本以 `exit 0` 收尾，只对未完成项发 `::warning::`，因此一次运行显示绿色不代表签到一定成功——**以日志、step summary 与微信消息为准**。

## 接口

均从客户端 `app.asar` 中核对并经线上实测，非网络流传版本。所有响应统一是 `{code, msg, data}` 信封，业务字段在 `data` 内，脚本用 `unwrap()` 统一解包。

```text
POST /v2/billing/meter/checkin-activity-status   查询今日签到状态
POST /v2/billing/meter/daily-checkin             领取签到积分（已签返回 code 10001）

GET  /v2/activity/growth/buddy/travel/status     查询旅行状态
POST /v2/activity/growth/buddy/travel/claim      领取已到家的旅行积分
GET  /v2/activity/growth/buddy/travel/config     获取可派目的地
POST /v2/activity/growth/buddy/travel/depart     派出一趟
```

时间戳字段（`arrive_at` / `depart_at` / `server_now`）为**秒级**，与毫秒混用会算出错误的剩余时间。

## 项目结构

```text
wb_auto.py                    云端执行主体：签到、领奖、派出、令牌到期检查、推送微信
refresh_token.py              本机一次性操作：导出 token / 写入 Secret（Windows + macOS）
refresh_token.exe             上者的免 Python 打包版（Windows x64，双击即用）
.github/workflows/daily.yml   cron 定义与执行步骤
wps/                          WPS 金山文档版（脚本 + 说明，见 wps/README.md）
```

> `refresh_token.exe` 由 `refresh_token.py` 经 PyInstaller 打包（零第三方依赖），
> 每次改动 `.py` 后需要重新构建，仅 Windows 需要。未做代码签名，SmartScreen 的提示属正常现象。
> **macOS 无需 exe**：直接用 `python3 refresh_token.py` 即可。

## 安全设计

- 仓库内不含任何凭据；token 只存在于 GitHub Secret 与运行时内存
- 解密助手只含固定代码，加密信封走 stdin，不经过 argv；密钥用后清零，不写临时文件
- 明文 token 不进入命令行、环境变量或日志；`--gh-repo` 模式下经 stdin 交给 `gh`
- 所有响应打印前过 `masked()` 脱敏，长字符串仅保留首尾若干字符
- 写操作仅在网络异常时有限重试，依赖服务端幂等避免重复提交

## 常见问题

**令牌过期、或收到黄/红到期提醒** — token 有效期 30 天（`refreshToken` 60 天），每次运行的推送都会带一行到期状态，剩 7 天转黄、3 天转红，不必自己记日子。续期步骤见 [令牌续期](#令牌续期)：本机导出新令牌 → 在 GitHub 网页 **Update** `WB_ACCESS_TOKEN` → 手动跑一次验证。若本机 `gh` 已登录，也可用 `--gh-repo OWNER/REPO` 一步写入；**`gh` 只是装了、没登录时会失败**，此时走网页方式。日志出现「认证被拒」时同样适用。

**fork 之后 Actions 页签是空的** — fork 来的仓库默认不启用 Actions。进 Actions 页签，点页面中间的 **I understand my workflows, go ahead and enable them** 启用一次即可。

**Actions 页签看不到 workflow** — 确认 `.github/workflows/daily.yml` 已入库，且仓库的 Actions 功能未被禁用。

**微信没收到消息** — 按通道类型对照日志与下方原因排查：

| 通道 | 日志行 | 典型失败原因 |
| --- | --- | --- |
| 企业微信 | `[企业微信]` | `errcode=93000/92001` → webhook 地址无效或复制不完整，检查 Secret `WECOM_WEBHOOK` |
| Server酱 | `[Server酱]` | `code!=0` → SendKey 有误；确认已在 sct.ftqq.com 完成服务号绑定 |
| PushPlus | `[PushPlus]` | `code!=200` → token 有误；确认已在 pushplus.plus 登录并关注公众号 |

当天 cron 有三个时段，某次失败后下一次运行会自动再推一遍当天结果（脚本幂等，不会重复领）。

**macOS 上提示「未找到凭据文件 / 未找到 WorkBuddy 客户端」** — 客户端版本或安装位置不同时，用环境变量显式指定：

```bash
WORKBUDDY_AUTH_FILE="$HOME/Library/Application Support/CodeBuddyExtension/Data/Public/auth/workbuddy-desktop.info" \
WORKBUDDY_EXE="/Applications/WorkBuddy.app/Contents/MacOS/Electron" \
python3 refresh_token.py
```

**偶发 302** — 高频轮询旅行状态时网关会短暂跳转，请求层已对 3xx 退避重试。按日频执行不会触发。

**非默认安装路径** — 用 `WORKBUDDY_EXE` 指定客户端，用 `WORKBUDDY_AUTH_FILE` 指定凭据文件。

## 已知限制

- 只覆盖签到与猫猫旅行；**不含**断登补签、连登兑换、盲盒、任务领取等成长中心其它玩法
- 依赖客户端私有接口，客户端升级可能改变接口或凭据格式，届时需更新脚本
- 仅支持 `sym-v1` / `suite 1` 格式的加密信封；其它格式会明确返回 `UNSUPPORTED_ENVELOPE` 而非发送无效请求
