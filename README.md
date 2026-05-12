# 校园电费监控工具 (Campus Power Monitor)

定时查询宿舍剩余电量，低于阈值时飞书告警，记录历史数据。

## 快速开始

```bash
# 1. 初始化配置
python3 monitor.py config --init

# 2. 编辑配置（修改栋数/房号/阈值等）
vim ~/.power-monitor/config.json

# 3. 一次性查询
python3 monitor.py check

# 4. 守护模式（持续监控）
python3 monitor.py watch

# 5. 查看历史
python3 monitor.py history
```

## 配置项

编辑 `~/.power-monitor/config.json`：

```json
{
  "api_base": "http://202.192.240.231",
  "endpoint": "/scp-api/electricity-recharge/getCurrentRemaining",
  "building": "0",
  "room": "0",
  "user_type_id": 1,
  "check_interval": 300,
  "alert_threshold": 20,
  "alert_cooldown": 3600,
  "notify_mode": "hermes",
  "feishu_chat_id": ""
}
```

| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| `building` | 栋数 | `12` |
| `room` | 房号 | `101` |
| `check_interval` | 查询间隔（秒） | `300` |
| `alert_threshold` | 告警阈值（度） | `20` |
| `alert_cooldown` | 告警冷却（秒） | `3600` |
| `notify_mode` | 通知方式 | `hermes` |

## 通知方式

- **`hermes`**（默认）：写入 JSONL 文件，由 Hermes cron job 中继到飞书
- **`feishu`**：直接调用飞书 API（需设 `FEISHU_APP_ID` / `FEISHU_APP_SECRET` 环境变量和 `feishu_chat_id`）
- **`none`**：不通知

## 部署为定时任务

### launchd (macOS)

```bash
cat > ~/Library/LaunchAgents/com.power-monitor.plist << 'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.power-monitor</string>
    <key>ProgramArguments</key>
    <array>
        <string>/usr/local/bin/python3</string>
        <string>/path/to/monitor.py</string>
        <string>watch</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>/tmp/power-monitor.log</string>
    <key>StandardErrorPath</key>
    <string>/tmp/power-monitor.err</string>
</dict>
</plist>
EOF

launchctl load ~/Library/LaunchAgents/com.power-monitor.plist
```

### cron (Linux/macOS)

```bash
# 每 10 分钟查一次
*/10 * * * * cd /path/to && python3 monitor.py check >> /tmp/power-monitor.log 2>&1
```

## Hermes 中继配置

如果用 `notify_mode: hermes`，需在 Hermes 中添加 cron job 来消费告警文件：

```
cronjob create:
  name: power-monitor-feishu-relay
  schedule: every 1m
  prompt: 读取 /tmp/power-monitor-alerts.jsonl，逐条发送到飞书 YOUR_CHAT_ID，然后清空文件
  deliver: local
```

## API 说明

接口：`POST /scp-api/electricity-recharge/getCurrentRemaining`

```
Content-Type: application/x-www-form-urlencoded
Body: userTypeID=1&building=0&room=0
```

响应：
```json
{"success": true, "code": 10000, "message": "查询成功", "data": 58.53}
```

无需认证，直接 POST 即可查询。

## 许可

MIT
