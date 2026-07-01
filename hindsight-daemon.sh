#!/bin/bash
# Hindsight API daemon wrapper for launchd.
# Runs the already-installed venv package directly, avoiding wrapper commands
# that may try to resolve packages from PyPI during startup.

set -euo pipefail

# Keep local and China API traffic out of Stash/Clash. Hindsight verifies the
# LLM endpoint at startup; proxying token-plan-cn can make a healthy endpoint
# look broken.
unset HTTP_PROXY HTTPS_PROXY http_proxy https_proxy
export NO_PROXY="${NO_PROXY:-localhost,127.0.0.1,::1,*.xiaomimimo.com,*.cn}"
export no_proxy="$NO_PROXY"

# Use cached HF models. The model files are already present on this Mac mini;
# online adapter checks have caused startup crashes on restricted networks.
export HF_HUB_OFFLINE=1
export TRANSFORMERS_OFFLINE=1

# Load profile environment
PROFILE_ENV="$HOME/.hindsight/profiles/hermes.env"
if [[ -f "$PROFILE_ENV" ]]; then
    while IFS='=' read -r key value; do
        # Skip comments and empty lines
        [[ "$key" =~ ^#.*$ || -z "$key" ]] && continue
        export "$key=$value"
    done < "$PROFILE_ENV"
fi

# Override idle timeout to 0 (never expire)
export HINDSIGHT_API_IDLE_TIMEOUT=0
export HINDSIGHT_API_DB_POOL_MAX="${HINDSIGHT_API_DB_POOL_MAX:-10}"

exec "$HOME/.hermes/hermes-agent/venv/bin/python" -m hindsight_api.main \
    --host 127.0.0.1 \
    --port 8100 \
    --log-level info \
    --no-access-log
