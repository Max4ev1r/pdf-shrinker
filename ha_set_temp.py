#!/usr/bin/env python3
"""Control Home Assistant climate entities."""
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
        print(f"HTTP Error {e.code}: {e.read().decode()}", file=sys.stderr)
        sys.exit(1)

def list_climates():
    states = api_call("/api/states")
    for e in states:
        if "climate" in e["entity_id"]:
            name = e["attributes"].get("friendly_name", "")
            temp = e["attributes"].get("temperature", "N/A")
            cur = e["attributes"].get("current_temperature", "N/A")
            mode = e["attributes"].get("hvac_mode", e["state"])
            print(f"{e['entity_id']} | {name} | 设定: {temp}° | 当前: {cur}° | 模式: {mode}")

def set_temperature(entity_id, temperature):
    result = api_call(f"/api/services/climate/set_temperature", method="POST", data={
        "entity_id": entity_id,
        "temperature": temperature
    })
    print(f"已设置 {entity_id} 温度为 {temperature}°C")
    print(f"响应: {json.dumps(result, ensure_ascii=False)}")

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("用法:")
        print("  python3 ha_set_temp.py list          # 列出所有空调")
        print("  python3 ha_set_temp.py set <entity> <temp>  # 设置温度")
        sys.exit(0)

    if sys.argv[1] == "list":
        list_climates()
    elif sys.argv[1] == "set" and len(sys.argv) >= 4:
        set_temperature(sys.argv[2], float(sys.argv[3]))
    else:
        print("参数错误")
        sys.exit(1)
