#!/usr/bin/env bash
set -euo pipefail
umask 077

mkdir -p "$HOME" "$XDG_CONFIG_HOME" "$XDG_DATA_HOME" "$XDG_CACHE_HOME" \
    "$XDG_RUNTIME_DIR" /state/orca /state/runtime
chmod 700 "$XDG_RUNTIME_DIR"
: > /state/runtime/app.pid

# Host networking is required for discovery; desktop servers must bind only
# loopback, and Xvfb must not expose an unauthenticated X server there.
python3 - <<'PY'
import socket
for port in (5901, 6080):
    with socket.socket() as listener:
        try:
            listener.bind(("127.0.0.1", port))
        except OSError as error:
            raise SystemExit(f"Development desktop port {port} is unavailable: {error}")
PY

if [[ ! -s /state/runtime/desktop-password ]]; then
    openssl rand -hex 4 > /state/runtime/desktop-password
fi
x11vnc -storepasswd "$(cat /state/runtime/desktop-password)" /state/runtime/vnc-password >/dev/null
xauth -f "$XAUTHORITY" add "$DISPLAY" . "$(openssl rand -hex 16)"

children=()
app_wrapper=
cleanup() {
    if [[ -s /state/runtime/app.pid ]]; then
        kill -TERM "$(cat /state/runtime/app.pid)" 2>/dev/null || true
    fi
    # Keep the display and D-Bus alive until the application has exited.
    if [[ -n "$app_wrapper" ]]; then wait "$app_wrapper" || true; fi
    for child in "${children[@]}"; do
        kill -TERM "$child" 2>/dev/null || true
    done
    wait || true
}
trap cleanup EXIT
trap 'exit 143' TERM
trap 'exit 130' INT

Xvfb "$DISPLAY" -screen 0 1920x1080x24 -nolisten tcp -auth "$XAUTHORITY" \
    > /state/runtime/xvfb.log 2>&1 &
children+=("$!")
for ((attempt = 0; attempt < 30; attempt++)); do
    if xdpyinfo >/dev/null 2>&1; then break; fi
    sleep 1
done
xdpyinfo >/dev/null
openbox > /state/runtime/openbox.log 2>&1 &
children+=("$!")
x11vnc -display "$DISPLAY" -listen 127.0.0.1 -rfbport 5901 \
    -rfbauth /state/runtime/vnc-password -forever -shared \
    > /state/runtime/vnc.log 2>&1 &
children+=("$!")
websockify --web /usr/share/novnc 127.0.0.1:6080 127.0.0.1:5901 \
    > /state/runtime/novnc.log 2>&1 &
children+=("$!")

# Record the app's PID, not dbus-run-session's, for status and graceful shutdown.
dbus-run-session -- bash -c '
    echo $$ > /state/runtime/app.pid
    exec /opt/orca/orca-slicer --datadir /state/orca "$@"
' bash "$@" > /state/runtime/orca.log 2>&1 &
children+=("$!")
app_wrapper=$!
wait "$app_wrapper"
