# homework-watcher v2

一个稳定优先的作业提醒网站。v2 是干净重建版本，只读扫描教学平台作业，写入本地 SQLite，并在网页显示当前待办。

## 当前能力

- FastAPI 网页：`GET /`
- SQLite assignments 表
- 统一 `ScanService`：Web 按钮和 CLI 共用同一套扫描链路
- 小雅 `known_courses.task_url` 扫描
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
      fake.py
      xiaoya.py
    settings.py
    status.py
  systemd/homework-watcher-v2.service
  tests/
  .env.example
  pyproject.toml
```

## 配置结构化学任务页

编辑 `config/platforms.yaml`：

```yaml
xiaoya:
  enabled: true
  base_url: "https://nankai.ai-augmented.com"
  known_courses:
    - course: "结构化学"
      course_id: "6902426124991620398"
      task_url: "https://nankai.ai-augmented.com/app/jx-web/mycourse/6902426124991620398/task"
```

v2 优先扫描 `known_courses`，不依赖从课程总页猜任务入口。

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

## 验收命令

```bash
cd v2
source .venv/bin/activate
python -m homework_watcher.cli health
python -m homework_watcher.cli scan-known-xiaoya
python -m homework_watcher.cli db-list
python -m homework_watcher.app
```

结构化学预期：

- 当前待办显示：`作业-08`
- 当前待办显示：`实习2 点阵理论`
- 当前待办不显示：`实习1 分子对称性`
- 当前待办不显示：`结构化学 / 结构化学 / 2026-07-31`

## 安全边界

- 不保存明文密码
- 不输出 cookie、token、Authorization
- 不绕过验证码
- 不自动提交作业
- 所有扫描只读
- 脱敏 dump 位于 `DEBUG_DUMP_DIR`，默认 `/tmp/hw-v2-debug`
