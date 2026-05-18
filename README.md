# homework-watcher

macOS 本地作业提醒系统。它只负责发现、记录、提醒和发送邮件日报，不自动提交作业，不绕过验证码，也不保存明文密码。

## 功能

- 使用 SQLite 保存作业、截止时间和提醒记录。
- 支持手动添加作业。
- 支持从粘贴文本解析作业标题、课程、平台、截止时间。
- 提供 `homework_watcher/platforms/changjiang_yuketang.py` 和 `homework_watcher/platforms/xiaoya.py` 两个平台适配器。
- 支持 Playwright 读取“长江雨课堂”和“小雅”页面上的作业信息。
- 支持 macOS 通知提醒。
- 支持输出“今日截止、明日截止、逾期未提交”汇总。
- 支持由 cron-job.org 触发 GitHub workflow 发送邮件日报。

## 安装

```bash
cd "/Users/zhangzimo/Library/Mobile Documents/com~apple~CloudDocs/homework-watcher"
./scripts/bootstrap.sh
```

如果你的终端提示 `externally-managed-environment`，这是 Homebrew Python 的系统环境保护。不要使用 `--break-system-packages`；本项目使用 `.venv` 本地虚拟环境安装。

安装后先激活虚拟环境：

```bash
source .venv/bin/activate
```

之后可以使用 `hw` 命令。也可以不激活，直接使用：

```bash
.venv/bin/hw --help
```

如果后续修改了项目代码，重新运行 `./scripts/bootstrap.sh` 即可把当前代码重新安装进 `.venv`。

默认数据库路径：

```text
~/.homework-watcher/homework.db
```

如果想指定数据库：

```bash
HW_DB_PATH=/path/to/homework.db hw list
```

## 常用命令

手动添加作业：

```bash
hw add "大学物理实验报告" --course "大学物理" --platform "长江雨课堂" --due "2026-05-15 23:59"
```

列出未完成作业：

```bash
hw list
```

标记完成：

```bash
hw done 3
```

从粘贴文本导入：

```bash
hw import-text
```

第一次使用平台自动检查前，先手动登录。程序只打开浏览器并复用本地浏览器登录态，不读取、不保存、不提交你的密码：

```bash
hw login changjiang-yuketang
hw login xiaoya
```

扫描平台作业并写入数据库：

```bash
hw scan
```

只扫描一个平台：

```bash
hw scan changjiang-yuketang
hw scan xiaoya
```

以统一 JSON 格式查看平台适配器输出：

```bash
hw scan xiaoya --json
```

每条平台适配器结果包含：

```json
{
  "title": "习题册第 3 章",
  "course": "高等数学",
  "platform": "小雅",
  "due_at": "2026-05-15T23:59:00",
  "status": "未提交",
  "url": "https://example.test/homework/1"
}
```

扫描后再检查提醒：

```bash
hw check --scan
```

也可以通过管道导入：

```bash
pbpaste | hw import-text
```

示例文本：

```text
课程：大学物理
平台：长江雨课堂
作业：大学物理实验报告
截止时间：2026-05-15 23:59
```

检查提醒并输出汇总：

```bash
hw check
```

只输出汇总：

```bash
hw summary
```

预览未完成作业日报邮件：

```bash
hw email-report --dry-run
```

日报里的作业条目按截止时间全局编号，并用方括号标注距今时间，例如：`1. 课程：有机化学 | 作业：有机化学作业 | 平台：线下 | 截止日期：2026-05-17 23:59 [距今：2天11小时59分钟]`。邮件中不会包含作业链接或状态。固定每周作业在日报中只展示本周内截止的条目。

补齐固定每周作业：

```bash
hw sync-recurring
```

当前内置两条固定规则：`定量化学分析作业` 每周二 23:59 截止，平台显示为 `飞书私信助教`；`有机化学作业` 每周日 23:59 截止，平台显示为 `线下`。`hw check` 会自动补齐未来 28 天内的固定作业，重复运行不会重复添加。

## 提醒规则

- 新作业第一次出现时提醒。
- 截止前 24 小时提醒。
- 截止前 6 小时提醒。
- 截止前 1 小时提醒。
- 已逾期且未完成时提醒。逾期提醒按天去重，避免定时任务重复刷屏。

`hw check` 每次只会对同一个作业触发当前最紧急且未发送过的一条临近截止提醒。

长江雨课堂中状态为“未开始/未开放”的作业会被标记为 `不可完成的作业`。这类任务不会出现在默认 `hw list`、提醒检查和每日汇总中；需要查看完整记录时使用 `hw list --all`。

## GitHub Actions 云端登录态

仓库包含手动建立云端浏览器登录态的 workflow：

```text
.github/workflows/cloud-platform-login.yml
```

在 GitHub 页面进入 `Actions`，选择 `Cloud platform login`，点击 `Run workflow`。第一次建议选择 `all`，并把 `hold_minutes` 设为 `30` 或更长。

运行到 `Open browser and print noVNC URL` 时，打开 workflow 右上角的运行摘要，里面会显示一个临时 noVNC 链接和 noVNC 密码。也可以在该步骤日志里找到：

```text
远程浏览器链接：https://....trycloudflare.com/vnc.html?autoconnect=1&resize=scale
本次 noVNC 密码：...
```

打开这个链接，输入 noVNC 密码，就会看到 GitHub runner 上的远程浏览器。手动登录小雅和长江雨课堂；程序不会读取、保存或提交你的密码，也不会绕过验证码。

如果希望 noVNC 密码不出现在日志里，可以在仓库 `Settings` -> `Secrets and variables` -> `Actions` 里增加：

```text
NOVNC_PASSWORD
```

配置后 workflow 不会打印密码，你打开 noVNC 时输入这个 secret 的值即可。VNC 密码最多 8 个字符，所以 `NOVNC_PASSWORD` 也必须不超过 8 个字符。登录完成后不要取消 workflow，等 `hold_minutes` 计时结束，workflow 会自动关闭远程浏览器并把云端浏览器登录态保存到 GitHub Actions cache。

注意：临时 noVNC 链接由 Cloudflare Quick Tunnel 提供，只在 workflow 运行期间有效。云端浏览器登录态包含 cookies/session，安全级别接近“已登录会话”。它保存在 GitHub Actions cache 中，不是明文密码，但仍应视为敏感数据。登录态也可能因为验证码、异地 IP 或平台风控而失效，失效后需要重新运行这个 workflow。

## GitHub Actions 邮件日报

仓库包含发送未完成作业日报的 workflow：

```text
.github/workflows/email-homework-report.yml
```

这个 workflow 只保留 `workflow_dispatch` 触发，用来手动调试，或让 cron-job.org 通过 GitHub REST API 定时触发。邮件正文会包含未完成作业统计、逾期未提交、今日截止、明日截止和未来待办。作业条目按截止时间全局编号，并用方括号标注距今时间，例如：`1. 课程：定量化学分析 | 作业：定量化学分析作业 | 平台：飞书私信助教 | 截止日期：2026-05-19 23:59 [距今：4天11小时59分钟]`。运行前会自动补齐内置固定每周作业，但日报中只展示本周内截止的固定作业。

需要在 GitHub 仓库中配置 `Settings` -> `Secrets and variables` -> `Actions`：

- Secret `SMTP_HOST`：SMTP 服务器地址。
- Secret `SMTP_PORT`：可选，默认 `587`。
- Secret `SMTP_USERNAME`：SMTP 登录用户名。
- Secret `SMTP_PASSWORD`：SMTP 密码或邮箱服务的 app password。
- Secret `EMAIL_FROM`：可选，默认使用 `SMTP_USERNAME`。
- Secret `EMAIL_TO`：收件邮箱；多个邮箱用英文逗号或分号分隔。
- Secret `SMTP_SSL`：可选；465 端口通常设为 `1`。
- Secret `SMTP_STARTTLS`：可选；默认 `1`，使用 587 端口时通常保持默认。

这个 workflow 会先尝试使用 GitHub Actions cache 中的云端浏览器登录态扫描小雅和长江雨课堂，再发送日报。如果扫描失败，会继续用云端数据库已有作业和本周内截止的内置固定作业发送日报，并在 Actions 日志里输出 warning。先本地预览日报内容：

```bash
hw email-report --dry-run
```

### 用 cron-job.org 定时触发

在 GitHub 创建一个 fine-grained personal access token，只授权这个仓库，Repository permissions 里给 `Actions` 设置 `Read and write`。不要把这个 token 写入仓库。

在 cron-job.org 新建 HTTP cronjob：

- URL: `https://api.github.com/repos/ZimoZhang1216/homework-watcher/actions/workflows/email-homework-report.yml/dispatches`
- Method: `POST`
- Schedule: 每天 08:00 中国时间。如果 cron-job.org 任务使用 UTC 时区，则填每天 `00:00`。
- Headers:

```text
Accept: application/vnd.github+json
Authorization: Bearer YOUR_GITHUB_TOKEN
X-GitHub-Api-Version: 2026-03-10
Content-Type: application/json
User-Agent: homework-watcher-cron-job
```

- Body:

```json
{"ref":"main"}
```

保存后可以在 cron-job.org 里手动执行一次。成功时 GitHub API 会返回 2xx，然后 GitHub 仓库的 `Actions` 页面会出现一次 `Email homework report` 运行记录。

## 托管版网站 MVP

仓库提供一个托管版 Web 入口，适合方案 B：每个同学注册自己的账号，在远程浏览器中手动登录小雅和长江雨课堂，服务端按用户隔离浏览器登录态、作业数据库和日报邮箱。

启动：

```bash
export SMTP_HOST="smtp.example.com"
export SMTP_USERNAME="sender@example.com"
export SMTP_PASSWORD="smtp-password-or-app-password"
export EMAIL_FROM="sender@example.com"
export HW_WEB_SECRET_KEY="change-this-to-a-long-random-secret"
export HW_WEB_ADMIN_TOKEN="change-this-admin-token"
export HW_WEB_NOVNC_URL="https://your-domain.example/vnc.html?autoconnect=1&resize=scale"
hw-web
```

Web 服务默认监听 `127.0.0.1:8080`。可以用环境变量调整：

```bash
HW_WEB_HOST=0.0.0.0 HW_WEB_PORT=8080 hw-web
```

远程登录依赖部署环境提供 Xvfb、x11vnc 和 noVNC。为了避免同学互相看到登录页面，当前 MVP 同一时间只允许一个远程登录会话。每个用户的浏览器资料和作业数据库保存在：

```text
~/.homework-watcher/web/users/<user-id>/
```

cron-job.org 可以每天触发托管网站的批量日报接口：

```text
POST https://your-domain.example/admin/run-daily?token=YOUR_HW_WEB_ADMIN_TOKEN
```

这个接口会为每个注册用户启动后台任务：扫描平台、补齐本周固定作业、发送日报。请只通过 HTTPS 暴露网站，并把 noVNC 放在同一层访问控制之后。

## 测试

```bash
python3 -m unittest discover -s tests
```

## 安全边界

本项目不会实现自动提交作业，不会绕过验证码，不会保存明文密码。Playwright 适配器只读取页面上已经登录会话中可见的作业信息，提交动作仍由用户手动完成。

## Playwright 登录态与错误处理

Playwright 浏览器资料默认保存在：

```text
~/.homework-watcher/browser-profiles/
```

`hw login <platform>` 使用有界面的 Chromium 打开平台页面。你在浏览器中手动登录后按回车，cookies/localStorage 等登录态会保存在本地浏览器资料目录中。后续 `hw scan` 和 `hw check --scan` 会复用这个登录态。

如果登录失效，程序会通过 macOS 通知提醒你重新运行：

```bash
hw login changjiang-yuketang
hw login xiaoya
```

如果平台页面结构变化，适配器会报出清晰错误，包含当前页面 URL 和已尝试的选择器。此时可以先用有界面模式确认页面内容：

```bash
hw scan changjiang-yuketang --headed
```

默认入口可以用环境变量覆盖：

```bash
HW_CHANGJIANG_YUKETANG_URL="https://changjiang.yuketang.cn/v2/web/index" hw scan changjiang-yuketang
HW_XIAOYA_URL="https://nankai.ai-augmented.com/app/jx-web/mycourse" hw scan xiaoya
```
