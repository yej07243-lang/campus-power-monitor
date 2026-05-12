#!/usr/bin/env python3
"""
校园电费监控工具 — Campus Power Monitor

定时查询宿舍剩余电量，低于阈值时发送飞书通知，记录历史数据。
API 无需认证，直接 POST 即可查询。

用法:
  python3 monitor.py check              # 一次性查询
  python3 monitor.py watch              # 守护模式，持续监控
  python3 monitor.py history            # 查看历史记录
  python3 monitor.py config             # 查看/生成配置文件
"""

import argparse
import json
import os
import sqlite3
import sys
import time
import urllib.request
import urllib.parse
from datetime import datetime, timezone, timedelta
from pathlib import Path

CST = timezone(timedelta(hours=8))

# ─── 默认配置 ───────────────────────────────────────────────
DEFAULT_CONFIG = {
    "api_base": "http://202.192.240.231",
    "endpoint": "/scp-api/electricity-recharge/getCurrentRemaining",
    "building": "0",
    "room": "0",
    "user_type_id": 1,
    "check_interval": 300,          # 秒，默认 5 分钟
    "alert_threshold": 20,           # 低于此度数（kWh）时告警
    "alert_cooldown": 3600,          # 告警冷却时间（秒），1 小时内不重复告警
    "notify_mode": "hermes",         # "hermes" | "feishu" | "none"
    "feishu_chat_id": "",            # 飞书群 chat_id（notify_mode=feishu 时使用）
    "hermes_alert_file": "/tmp/power-monitor-alerts.jsonl",
    "db_path": "~/.power-monitor/history.db",
    "log_level": "INFO",
}

CONFIG_DIR = Path.home() / ".power-monitor"
CONFIG_FILE = CONFIG_DIR / "config.json"


# ─── 配置加载 ───────────────────────────────────────────────
def load_config() -> dict:
    """加载配置：默认 → 配置文件 → 环境变量覆盖."""
    config = DEFAULT_CONFIG.copy()

    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE) as f:
                config.update(json.load(f))
        except (json.JSONDecodeError, IOError) as e:
            print(f"⚠ 配置文件读取失败: {e}", file=sys.stderr)

    # 环境变量覆盖
    env_map = {
        "POWER_API_BASE": "api_base",
        "POWER_BUILDING": "building",
        "POWER_ROOM": "room",
        "POWER_INTERVAL": ("check_interval", int),
        "POWER_THRESHOLD": ("alert_threshold", float),
        "POWER_NOTIFY_MODE": "notify_mode",
        "POWER_FEISHU_CHAT_ID": "feishu_chat_id",
    }
    for env_key, map_to in env_map.items():
        val = os.environ.get(env_key)
        if val is not None:
            if isinstance(map_to, tuple):
                key, converter = map_to
                config[key] = converter(val)
            else:
                config[map_to] = val

    # 展开 ~ 路径
    config["db_path"] = os.path.expanduser(config["db_path"])
    return config


def init_config():
    """初始化配置文件."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    if not CONFIG_FILE.exists():
        with open(CONFIG_FILE, "w") as f:
            json.dump(DEFAULT_CONFIG, f, indent=2, ensure_ascii=False)
        print(f"✓ 配置文件已创建: {CONFIG_FILE}")
    else:
        print(f"配置文件已存在: {CONFIG_FILE}")


# ─── API 查询 ───────────────────────────────────────────────
def query_balance(config: dict) -> dict:
    """
    查询剩余电量。返回 API 原始响应。
    成功: {"success": True, "data": 58.53}
    失败: {"success": False, "error": "..."}
    """
    url = f"{config['api_base']}{config['endpoint']}"
    data = urllib.parse.urlencode({
        "userTypeID": str(config["user_type_id"]),
        "building": config["building"],
        "room": config["room"],
    }).encode()

    try:
        req = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = json.loads(resp.read().decode())
            if body.get("success"):
                return {"success": True, "data": body["data"], "message": body.get("message", "")}
            else:
                return {"success": False, "error": body.get("message", "未知错误")}
    except urllib.error.HTTPError as e:
        return {"success": False, "error": f"HTTP {e.code}"}
    except Exception as e:
        return {"success": False, "error": str(e)}


# ─── 数据库 ─────────────────────────────────────────────────
def get_db(config: dict) -> sqlite3.Connection:
    db_path = config["db_path"]
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            kwh REAL NOT NULL,
            building TEXT,
            room TEXT
        )
    """)
    conn.commit()
    return conn


def save_record(conn: sqlite3.Connection, config: dict, kwh: float):
    now = datetime.now(CST).isoformat()
    conn.execute(
        "INSERT INTO records (timestamp, kwh, building, room) VALUES (?, ?, ?, ?)",
        (now, kwh, config["building"], config["room"]),
    )
    conn.commit()


def get_last_kwh(conn: sqlite3.Connection, config: dict) -> float | None:
    row = conn.execute(
        "SELECT kwh FROM records WHERE building=? AND room=? ORDER BY id DESC LIMIT 1",
        (config["building"], config["room"]),
    ).fetchone()
    return row[0] if row else None


# ─── 通知 ───────────────────────────────────────────────────
def send_alert(config: dict, kwh: float, last_kwh: float | None):
    """发送低电量告警."""
    ts = datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")
    building = config["building"]
    room = config["room"]
    threshold = config["alert_threshold"]

    change = ""
    if last_kwh is not None:
        delta = kwh - last_kwh
        if delta < 0:
            change = f"\n较上次: {delta:+.2f} 度"
        else:
            change = f"\n较上次: +{delta:.2f} 度"

    text = (
        f"⚡ **电费告警**\n\n"
        f"栋数：{building} 栋\n"
        f"房号：{room}\n"
        f"当前剩余：**{kwh:.2f} 度**（阈值：{threshold} 度）{change}\n"
        f"⏰ 检测时间：{ts}"
    )

    mode = config["notify_mode"]
    if mode == "hermes":
        alert_file = config["hermes_alert_file"]
        entry = {
            "ts": datetime.now(CST).isoformat(),
            "type": "power_low",
            "title": f"⚡ 电费告警 — {building}栋{room}",
            "text": text,
        }
        try:
            with open(alert_file, "a") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            print(f"  📤 告警已写入 {alert_file}")
        except Exception as e:
            print(f"  ⚠ 写入告警文件失败: {e}", file=sys.stderr)

    elif mode == "feishu":
        chat_id = config.get("feishu_chat_id", "")
        if not chat_id:
            print("  ⚠ 未配置 feishu_chat_id", file=sys.stderr)
            return
        _send_feishu(config, chat_id, text)

    elif mode == "none":
        pass
    else:
        print(f"  ⚠ 未知通知模式: {mode}", file=sys.stderr)


def _send_feishu(config: dict, chat_id: str, text: str):
    """直接通过飞书 API 发送消息."""
    app_id = os.environ.get("FEISHU_APP_ID")
    app_secret = os.environ.get("FEISHU_APP_SECRET")
    if not app_id or not app_secret:
        print("  ⚠ 缺少 FEISHU_APP_ID / FEISHU_APP_SECRET 环境变量", file=sys.stderr)
        return

    # 获取 token
    try:
        req = urllib.request.Request(
            "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
            data=json.dumps({"app_id": app_id, "app_secret": app_secret}).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            token_data = json.loads(resp.read().decode())
        token = token_data.get("tenant_access_token", "")
        if not token:
            print(f"  ⚠ 获取飞书 token 失败: {token_data}", file=sys.stderr)
            return
    except Exception as e:
        print(f"  ⚠ 飞书认证异常: {e}", file=sys.stderr)
        return

    # 发送消息
    try:
        payload = {
            "receive_id": chat_id,
            "msg_type": "text",
            "content": json.dumps({"text": text}),
        }
        req = urllib.request.Request(
            f"https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id",
            data=json.dumps(payload).encode(),
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read().decode())
        if result.get("code") == 0:
            print(f"  📤 飞书通知已发送")
        else:
            print(f"  ⚠ 飞书发送失败: {result}", file=sys.stderr)
    except Exception as e:
        print(f"  ⚠ 飞书发送异常: {e}", file=sys.stderr)


# ─── 命令 ───────────────────────────────────────────────────
def cmd_check(config: dict) -> int:
    """一次性查询."""
    result = query_balance(config)
    building = config["building"]
    room = config["room"]

    if result["success"]:
        kwh = result["data"]
        print(f"✅ {building}栋{room} — 剩余电量: {kwh} 度")

        conn = get_db(config)
        last_kwh = get_last_kwh(conn, config)
        save_record(conn, config, kwh)
        conn.close()

        if kwh <= config["alert_threshold"]:
            print(f"  🚨 低于阈值 ({config['alert_threshold']} 度)，触发告警")
            send_alert(config, kwh, last_kwh)

        if last_kwh is not None:
            delta = kwh - last_kwh
            print(f"  📊 较上次: {delta:+.2f} 度")
        return 0
    else:
        print(f"❌ 查询失败: {result['error']}")
        return 1


def cmd_watch(config: dict) -> int:
    """守护模式，持续监控."""
    interval = config["check_interval"]
    threshold = config["alert_threshold"]
    cooldown = config["alert_cooldown"]
    building = config["building"]
    room = config["room"]

    print(f"🔍 电费监控启动 — {building}栋{room}")
    print(f"   间隔: {interval}s, 阈值: {threshold} 度, 冷却: {cooldown}s")
    print(f"   按 Ctrl+C 停止\n")

    conn = get_db(config)
    last_alert_time: float = 0

    try:
        while True:
            ts = datetime.now(CST).strftime("%H:%M:%S")
            result = query_balance(config)

            if result["success"]:
                kwh = result["data"]
                last_kwh = get_last_kwh(conn, config)
                save_record(conn, config, kwh)

                delta_str = ""
                if last_kwh is not None:
                    delta_str = f" ({last_kwh - kwh:+.2f})"

                status = "🟢" if kwh > threshold else "🔴"
                print(f"[{ts}] {status} {kwh:.2f} 度{delta_str}")

                if kwh <= threshold:
                    now = time.time()
                    if now - last_alert_time >= cooldown:
                        print(f"  🚨 低电量告警!")
                        send_alert(config, kwh, last_kwh)
                        last_alert_time = now
            else:
                print(f"[{ts}] ⚠ 查询失败: {result['error']}")

            time.sleep(interval)

    except KeyboardInterrupt:
        print("\n👋 监控已停止")
        conn.close()
        return 0


def cmd_history(config: dict) -> int:
    """查看历史记录."""
    conn = get_db(config)
    building = config["building"]
    room = config["room"]

    rows = conn.execute(
        "SELECT timestamp, kwh FROM records WHERE building=? AND room=? ORDER BY id DESC LIMIT 50",
        (building, room),
    ).fetchall()
    conn.close()

    if not rows:
        print("暂无记录")
        return 0

    print(f"📊 {building}栋{room} 电费历史 (最近 50 条):")
    print(f"{'时间':<22} {'电量':>8}")
    print("-" * 32)
    for ts, kwh in rows:
        # 格式化为可读时间
        try:
            dt = datetime.fromisoformat(ts)
            ts_str = dt.strftime("%m-%d %H:%M")
        except Exception:
            ts_str = ts[:16]
        bar = "█" * min(int(kwh / 2), 30)
        print(f"{ts_str:<16} {kwh:>6.2f} 度  {bar}")

    # 统计
    first = rows[-1]
    last = rows[0]
    if len(rows) >= 2:
        total_delta = last[1] - first[1]
        print(f"\n📈 最早: {first[1]:.2f} → 最新: {last[1]:.2f} (变化: {total_delta:+.2f} 度, 共 {len(rows)} 条)")

    return 0


def cmd_config(args) -> int:
    """查看或初始化配置."""
    if args.init:
        init_config()
        return 0

    config = load_config()
    print(f"配置文件: {CONFIG_FILE}")
    print(f"存在: {'是' if CONFIG_FILE.exists() else '否'}")
    print()
    for k, v in config.items():
        if "secret" in k.lower() or "token" in k.lower():
            v = "***"
        print(f"  {k}: {v}")
    return 0


# ─── CLI ────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(
        prog="power-monitor",
        description="校园电费监控工具 — 定时查询电量，低电量飞书告警",
    )

    sub = parser.add_subparsers(dest="command", help="子命令")

    p_check = sub.add_parser("check", help="一次性查询电费")
    p_check.set_defaults(func=lambda args: cmd_check(load_config()))

    p_watch = sub.add_parser("watch", help="守护模式：持续监控")
    p_watch.set_defaults(func=lambda args: cmd_watch(load_config()))

    p_hist = sub.add_parser("history", help="查看历史记录")
    p_hist.set_defaults(func=lambda args: cmd_history(load_config()))

    p_cfg = sub.add_parser("config", help="查看/初始化配置")
    p_cfg.add_argument("--init", action="store_true", help="初始化配置文件")
    p_cfg.set_defaults(func=cmd_config)

    args = parser.parse_args()
    if args.command is None:
        parser.print_help()
        return 0
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
