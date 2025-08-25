#!/usr/bin/env sh
set -e

# Defaults
PLAYWRIGHT_BROWSERS_PATH=${PLAYWRIGHT_BROWSERS_PATH:-/ms-playwright}
APP_UID=${APP_UID:-1000}
APP_USER=${APP_USER:-flow}
HOME=${HOME:-/app}

# Ensure HOME and cache dir exist
mkdir -p "$HOME" "$HOME/.cache" || true

# If shared browsers exist, symlink into per-user cache so Playwright's per-user lookup works
if [ -d "$PLAYWRIGHT_BROWSERS_PATH" ]; then
  ln -sfn "$PLAYWRIGHT_BROWSERS_PATH" "$HOME/.cache/ms-playwright" || true
fi

# Also create symlink at the system home directory for the runtime UID (what uv_os_homedir/getpwuid returns)
SYS_HOME=""
if [ -r /etc/passwd ]; then
  SYS_HOME=$(awk -F: -v uid="${APP_UID}" '$3==uid{print $6; exit}' /etc/passwd || true)
fi
if [ -z "$SYS_HOME" ] && [ -n "$APP_USER" ]; then
  SYS_HOME="/home/${APP_USER}"
fi
if [ -n "$SYS_HOME" ]; then
  mkdir -p "$SYS_HOME/.cache" || true
  if [ -d "$PLAYWRIGHT_BROWSERS_PATH" ]; then
    ln -sfn "$PLAYWRIGHT_BROWSERS_PATH" "$SYS_HOME/.cache/ms-playwright" || true
  fi
  chown -R ${APP_UID}:${APP_UID} "$SYS_HOME" 2>/dev/null || true
fi

# If gosu is available, use it to run the command as the unprivileged user; otherwise run directly
if command -v gosu >/dev/null 2>&1; then
  exec gosu "${APP_UID}" "$@"
else
  exec "$@"
fi