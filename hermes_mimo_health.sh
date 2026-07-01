#!/usr/bin/env bash
set -u

CONFIG_PATH="${HERMES_CONFIG_PATH:-$HOME/.hermes/config.yaml}"
ENV_PATH="${HERMES_ENV_PATH:-$HOME/.hermes/.env}"
EXPECTED_PROVIDER="xiaomi"
EXPECTED_MODEL="mimo-v2.5-pro"
failed=0

check_ok() {
  printf 'OK   %s\n' "$1"
}

check_fail() {
  printf 'FAIL %s\n' "$1"
  failed=1
}

config_value() {
  local key="$1"
  awk -v wanted="$key" '
    /^model:/ { in_model=1; next }
    in_model && /^[^[:space:]]/ { in_model=0 }
    in_model && $1 == wanted ":" {
      value=$2
      gsub(/^["'\'']|["'\'']$/, "", value)
      print value
      exit
    }
  ' "$CONFIG_PATH" 2>/dev/null
}

if [[ ! -r "$CONFIG_PATH" ]]; then
  check_fail "config readable at $CONFIG_PATH"
else
  provider="$(config_value provider)"
  model="$(config_value default)"

  if [[ "$provider" == "$EXPECTED_PROVIDER" ]]; then
    check_ok "provider=$provider"
  else
    check_fail "provider=$provider expected=$EXPECTED_PROVIDER"
  fi

  if [[ "$model" == "$EXPECTED_MODEL" ]]; then
    check_ok "model=$model"
  else
    check_fail "model=$model expected=$EXPECTED_MODEL"
  fi
fi

serper_key="${SERPER_API_KEY:-}"
if [[ -z "$serper_key" && -r "$ENV_PATH" ]]; then
  serper_key="$(awk -F= '/^[[:space:]]*SERPER_API_KEY[[:space:]]*=/ {print $2; exit}' "$ENV_PATH" | tr -d '"'\'"'"'[:space:]')"
fi
if [[ -z "$serper_key" ]] && command -v launchctl >/dev/null 2>&1; then
  serper_key="$(launchctl getenv SERPER_API_KEY 2>/dev/null | tr -d '[:space:]')"
fi

if [[ -n "$serper_key" ]]; then
  check_ok "SERPER_API_KEY present"
else
  check_fail "SERPER_API_KEY missing"
fi

legacy_minimax_enabled="${HERMES_ENABLE_LEGACY_MINIMAX_TOOLS:-}"
if [[ -z "$legacy_minimax_enabled" && -r "$ENV_PATH" ]]; then
  legacy_minimax_enabled="$(awk -F= '/^[[:space:]]*HERMES_ENABLE_LEGACY_MINIMAX_TOOLS[[:space:]]*=/ {print $2; exit}' "$ENV_PATH" | tr -d '"'\'"'"'[:space:]')"
fi
if [[ -z "$legacy_minimax_enabled" ]] && command -v launchctl >/dev/null 2>&1; then
  legacy_minimax_enabled="$(launchctl getenv HERMES_ENABLE_LEGACY_MINIMAX_TOOLS 2>/dev/null | tr -d '[:space:]')"
fi

if [[ "$legacy_minimax_enabled" == "1" ]]; then
  check_fail "legacy MiniMax direct tools enabled"
else
  check_ok "legacy MiniMax direct tools disabled"
fi

if command -v hermes >/dev/null 2>&1; then
  mcp_list="$(hermes mcp list 2>/dev/null || true)"
  tools_list="$(hermes tools list 2>/dev/null || true)"

  if printf '%s\n' "$mcp_list" | grep -q 'expert-tools'; then
    check_ok "expert-tools reachable"
  else
    check_fail "expert-tools not found in hermes mcp list"
  fi

  if printf '%s\n%s\n' "$mcp_list" "$tools_list" | grep -qi 'minimax'; then
    check_fail "MiniMax appears in active MCP/tool list"
  else
    check_ok "MiniMax absent from active MCP/tool list"
  fi
else
  check_fail "hermes command not found"
fi

if [[ "$failed" -eq 0 ]]; then
  printf 'HERMES_MIMO_HEALTH_OK\n'
else
  printf 'HERMES_MIMO_HEALTH_FAIL\n'
fi

exit "$failed"
