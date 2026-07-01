#!/usr/bin/env python3
"""Home Assistant controller - reads token from ~/.hermes/secrets/ha_token.txt."""
import json
import os
import sys
import urllib.request
import urllib.error
from pathlib import Path

TOKEN_FILE = Path(os.environ.get("HA_TOKEN_FILE", "/Users/max/.hermes/secrets/ha_token.txt"))
LEGACY_TOKEN_FILE = Path("/Users/max/.hermes/ha_token.txt")
HA_URL = "http://localhost:8123"

def get_token():
    for path in (TOKEN_FILE, LEGACY_TOKEN_FILE):
        if path.exists():
            return path.read_text(encoding="utf-8").strip()
    raise FileNotFoundError(f"Home Assistant token not found: {TOKEN_FILE}")

def api_call(path, method="GET", data=None):
    token = get_token()
    url = f"{HA_URL}{path}"
    req = urllib.request.Request(url, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    if data:
        req.add_header("Content-Type", "application/json")
        req.data = json.dumps(data).encode()
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        print(f"HTTP Error {e.code}: {body}", file=sys.stderr)
        sys.exit(1)

def list_climates():
    states = api_call("/api/states")
    climates = [e for e in states if "climate" in e["entity_id"]]
    for c in climates:
        name = c["attributes"].get("friendly_name", "")
        temp = c["attributes"].get("temperature", "N/A")
        cur = c["attributes"].get("current_temperature", "N/A")
        mode = c["state"]
        print(f"{c['entity_id']} | {name} | 设定: {temp}° | 当前: {cur}° | 模式: {mode}")
    return climates

def set_temperature(entity_id, temperature):
    result = api_call("/api/services/climate/set_temperature", method="POST", data={
        "entity_id": entity_id,
        "temperature": temperature
    })
    print(f"✅ 已设置 {entity_id} 温度为 {temperature}°C")

def turn_on(entity_id):
    result = api_call("/api/services/climate/turn_on", method="POST", data={
        "entity_id": entity_id
    })
    print(f"✅ 已开启 {entity_id}")

def turn_off(entity_id):
    result = api_call("/api/services/climate/turn_off", method="POST", data={
        "entity_id": entity_id
    })
    print(f"✅ 已关闭 {entity_id}")

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("用法: python3 ha_control.py <command> [args]")
        print("  list                    - 列出所有空调")
        print("  set <entity> <temp>     - 设置温度")
        print("  on <entity>             - 开启空调")
        print("  off <entity>            - 关闭空调")
        sys.exit(0)

    cmd = sys.argv[1]
    if cmd == "list":
        list_climates()
    elif cmd == "set" and len(sys.argv) >= 4:
        set_temperature(sys.argv[2], float(sys.argv[3]))
    elif cmd == "on" and len(sys.argv) >= 3:
        turn_on(sys.argv[2])
    elif cmd == "off" and len(sys.argv) >= 3:
        turn_off(sys.argv[2])
    else:
        print("参数错误")
        sys.exit(1)
