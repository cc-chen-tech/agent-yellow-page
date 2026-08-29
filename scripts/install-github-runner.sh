#!/usr/bin/env bash
# Install and register a GitHub Actions self-hosted runner for this repo.
# Run on the deploy server (e.g. /opt/agent-yellow-page is already set up).
#
# Usage:
#   1. Go to https://github.com/cc-chen-tech/agent-yellow-page/settings/actions/runners/new
#   2. Pick Linux / x64
#   3. Copy the registration token (it looks like "ABC123XYZ...")
#   4. Run this script with the token:
#        sudo ./scripts/install-github-runner.sh ABC123XYZ
#
# Optional flags:
#   --name <runner-name>     default: hostname
#   --labels a,b,c           default: yellowpage-deploy
#   --runner-dir <path>      default: /opt/actions-runner

set -euo pipefail

REPO="cc-chen-tech/agent-yellow-page"
RUNNER_DIR="${RUNNER_DIR:-/opt/actions-runner}"
RUNNER_LABELS="${RUNNER_LABELS:-yellowpage-deploy}"
RUNNER_NAME="${RUNNER_NAME:-$(hostname)}"
TOKEN="${1:-}"

if [[ -z "$TOKEN" ]]; then
  cat <<EOF >&2
ERROR: missing registration token.

Get one from:
  https://github.com/${REPO}/settings/actions/runners/new
(pick Linux / x64, copy the token, it expires in ~1h)

Then re-run:
  sudo $0 <TOKEN>
EOF
  exit 1
fi

if [[ -e "$RUNNER_DIR" ]]; then
  echo "WARN: $RUNNER_DIR already exists; reusing" >&2
else
  echo "==> creating $RUNNER_DIR"
  mkdir -p "$RUNNER_DIR"
  cd "$RUNNER_DIR"
  echo "==> downloading runner package"
  curl -fsSL -o runner.tar.gz \
    "https://github.com/actions/runner/releases/download/v2.319.1/actions-runner-linux-x64-2.319.1.tar.gz"
  tar -xzf runner.tar.gz
  rm runner.tar.gz
  ./bin/installdependencies.sh
fi

cd "$RUNNER_DIR"

# If already configured, exit early
if [[ -f ".runner" ]]; then
  echo "==> already configured; re-registering"
  ./config.sh remove --token "$TOKEN" || true
fi

echo "==> configuring runner name=$RUNNER_NAME labels=$RUNNER_LABELS"
./config.sh \
  --unattended \
  --replace \
  --url "https://github.com/${REPO}" \
  --token "$TOKEN" \
  --name "$RUNNER_NAME" \
  --labels "$RUNNER_LABELS" \
  --work "_work"

echo "==> installing systemd unit"
./svc.sh install root
./svc.sh start

echo
echo "OK — runner registered and started. Check:"
echo "  https://github.com/${REPO}/settings/actions/runners"
echo "  systemctl status actions.runner.*"
