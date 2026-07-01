#!/usr/bin/env python3
"""Sync xiaomi key and run TTS in one shot."""
import json, subprocess, sys

# Sync key from .env to auth.json
with open('/Users/max/.hermes/.env') as f:
    for line in f:
        line = line.strip()
        if line.startswith('XIAOMI_API_KEY=') and not line.startswith('#'):
            api_key = line.split('=', 1)[1].strip().strip('"').strip("'")
            with open('/Users/max/.hermes/auth.json') as f2:
                data = json.load(f2)
            data['credential_pool']['xiaomi'][0]['access_token'] = api_key
            with open('/Users/max/.hermes/auth.json', 'w') as f2:
                json.dump(data, f2, indent=2, ensure_ascii=False)
            print("synced OK, key length:", len(api_key))
            break

# Run TTS
text = sys.argv[1] if len(sys.argv) > 1 else "你好"
result = subprocess.run(
    ['python3', 'scripts/dilraba-voice-clone/yujie_tts.py', text],
    cwd='/Users/max/.hermes',
    capture_output=True, text=True
)
print(result.stdout)
if result.returncode != 0:
    print(result.stderr)
    sys.exit(1)
