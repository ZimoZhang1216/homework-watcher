# homework-watcher v2

一个稳定优先的作业提醒网站。v2 是干净重建版本，只读扫描教学平台作业，写入本地 SQLite，并在网页显示当前待办。

## 当前能力

- FastAPI 网页：`GET /`
- 本地账号密码登录
- SQLite assignments 表
- 统一 `ScanService`：Web 按钮和 CLI 共用同一套扫描链路
- 长江雨课堂课程作业扫描
- 小雅 `known_courses.task_url` 通用扫描
- Playwright 持久化登录态
- CLI 诊断命令
- scan 日志和脱敏 debug dump
- systemd service 示例

## 项目结构

```text
v2/
  config/platforms.yaml
  homework_watcher/
    app.py
    candidates.py
    cli.py
    config_loader.py
    database.py
    debug_dump.py
    logging_utils.py
    models.py
    scan_service.py
    scanners/
      base.py
      changjiang_yuketang.py
      fake.py
      xiaoya.py
    settings.py
    status.py
  systemd/homework-watcher-v2.service
  tests/
  .env.example
  pyproject.toml
```

## 配置平台扫描

编辑 `config/platforms.yaml`：

```yaml
changjiang-yuketang:
  enabled: true
  base_url: "https://changjiang.yuketang.cn/v2/web/index"

xiaoya:
  enabled: true
  base_url: "https://nankai.ai-augmented.com"
  known_courses:
    - course: "课程名"
      course_id: "课程 ID"
      task_url: "https://nankai.ai-augmented.com/app/jx-web/mycourse/课程 ID/task"
```

长江雨课堂会从课程列表进入每门课解析作业。小雅优先扫描 `known_courses`，不依赖从课程总页猜任务入口。

## 本地运行

```bash
cd v2
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
python -m playwright install chromium
cp .env.example .env
python -m homework_watcher.cli health
```

手动登录小雅：

```bash
python -m homework_watcher.cli login-xiaoya
```

程序只打开浏览器保存登录态，不读取、不保存、不提交密码，不绕过验证码。

诊断扫描小雅 known courses：

```bash
python -m homework_watcher.cli scan-known-xiaoya
python -m homework_watcher.cli db-list
```

启动网页：

```bash
python -m homework_watcher.app
```

打开 `http://127.0.0.1:8080/`，点击“立即扫描”。

## ECS systemd 部署

示例路径使用 `/opt/homework-watcher-v2`：

```bash
sudo mkdir -p /opt/homework-watcher-v2
sudo rsync -a --delete v2/ /opt/homework-watcher-v2/
cd /opt/homework-watcher-v2
sudo python3.11 -m venv .venv
sudo .venv/bin/python -m pip install -e .
sudo .venv/bin/python -m playwright install chromium
sudo cp .env.example /etc/homework-watcher-v2.env
sudo cp systemd/homework-watcher-v2.service /etc/systemd/system/homework-watcher-v2.service
sudo systemctl daemon-reload
sudo systemctl enable --now homework-watcher-v2
```

查看服务和日志：

```bash
systemctl status homework-watcher-v2 --no-pager
journalctl -u homework-watcher-v2 -f
tail -n 160 /opt/homework-watcher-v2/logs/scan.log
```

健康检查：

```bash
curl http://127.0.0.1:8080/health
```

如需公网访问，建议用 Nginx 反向代理到 `127.0.0.1:8080`，再按阿里云安全组开放 80/443。

### 阿里云一键脚本

仓库根目录提供 v2 专用脚本：

```bash
bash deploy/install-aliyun-v2.sh
```

默认部署到 `http://8.141.109.80/`，停止旧版 `homework-watcher-web` 服务但保留旧数据目录 `/var/lib/homework-watcher/web`。如果旧版用户 1 的小雅浏览器 profile 存在，脚本会在 v2 profile 为空时复制一次，尽量复用已登录状态。

脚本部署后可用：

```bash
systemctl status homework-watcher-v2 --no-pager
curl http://127.0.0.1:8080/health
homework-watcher-v2-login-xiaoya
```

`homework-watcher-v2-login-xiaoya` 会在服务器的 noVNC 桌面中打开小雅登录浏览器；noVNC 地址会写入 `/root/homework-watcher-v2-deployment.txt`。

## 网站账号和平台登录

v2 首页需要先注册网站账号并登录。网站密码使用 PBKDF2-SHA256 哈希保存，不保存明文密码。每个网站账号只会看到自己的作业记录。

登录后首页提供“长江雨课堂登录”和“小雅登录”按钮。点击后服务会在服务器上的有界面 Chromium 中打开对应平台，并跳转到 noVNC 远程浏览器页面。手动登录完成后点击“我已完成登录”，服务会关闭浏览器并保留登录态。

平台 profile 按网站用户名隔离：

```text
data/playwright-user-data/users/<username>/changjiang-yuketang
data/playwright-user-data/users/<username>/xiaoya
```

旧版本中已有的作业记录会迁移到 `default` 所属空间；新注册用户不会看到这些记录。

点击首页“立即扫描”会读取当前网站账号的长江雨课堂和小雅登录态，解析作业并写入该账号自己的 `assignments` 记录；`进行中`、`未完成`、`未提交` 等当前任务会进入待办。长江雨课堂中 `未开始/未开放` 会标记为不可完成，不进入当前待办。

## 验收命令

```bash
cd v2
source .venv/bin/activate
python -m homework_watcher.cli health
python -m homework_watcher.cli scan --platform changjiang-yuketang
python -m homework_watcher.cli scan-known-xiaoya
python -m homework_watcher.cli db-list
python -m homework_watcher.app
```

小雅验收重点：

- `scan-known-xiaoya` 输出 `xiaoya_assignment_count` 和 `xiaoya_todo_count`。
- 当前任务状态如 `进行中`、`未提交`、`待完成`、`未完成` 会进入待办。
- 已完成或已截止任务会保留在所有记录里，但不进入当前待办。

长江雨课堂验收重点：

- `scan --platform changjiang-yuketang` 输出 `candidates_count`。
- `未作答/未提交` 会进入待办。
- `得分/已提交/已完成` 与 `未开始/未开放` 不进入当前待办。

## 安全边界

- 不保存明文密码
- 不输出 cookie、token、Authorization
- 不绕过验证码
- 不自动提交作业
- 所有扫描只读
- 脱敏 dump 位于 `DEBUG_DUMP_DIR`，默认 `/tmp/hw-v2-debug`
