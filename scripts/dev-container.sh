#!/usr/bin/env bash
# Build and run OrcaSlicer without sharing the installed application's state.
set -euo pipefail

repo=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
state="$repo/build-dev-state"
workspace="$state/workspace"
container="orca-dev-$(printf '%s' "$repo" | sha256sum | cut -c1-12)"

usage() {
    cat <<'EOF'
Usage: bash scripts/dev-container.sh COMMAND [ARGUMENTS]
  image         Prepare the builder and isolated desktop image
  build [opts]  Build Release and tests (opts are build_linux.sh options)
  test [opts]   Run CTest in the builder (opts are CTest options)
  start         Start the isolated GUI at http://127.0.0.1:6080/vnc.html
  stop          Stop only this checkout's GUI; preserve development state
  status        Show the container, running binary, and data directory

State and build outputs: build-dev-state/ (gitignored)
Models: build-dev-state/exchange/
Desktop password: build-dev-state/runtime/desktop-password
Podman images and layers: build-dev-state/podman-storage/
Podman temporary files: build-dev-state/podman-tmp/
ORCA_DEV_JOBS sets compile jobs (default 2).
ORCA_DEV_MEMORY sets the memory limit (default half of available host RAM).
EOF
}

command=${1:-help}
shift || true
case "$command" in
    help|-h|--help) usage; exit 0 ;;
    image|build|test|start|stop|status) ;;
    *) usage >&2; exit 2 ;;
esac
if [[ "$command" != build && "$command" != test && $# != 0 ]]; then
    echo "Unexpected arguments for $command." >&2
    exit 2
fi
if [[ "$(uname -s)" != Linux ]]; then
    echo "This launcher requires Linux and rootless Podman." >&2
    exit 1
fi
command -v podman >/dev/null || { echo "Install Podman first." >&2; exit 1; }
if [[ "$(podman info --format '{{.Host.Security.Rootless}}')" != true ]]; then
    echo "Run this launcher as your regular user with rootless Podman." >&2
    exit 1
fi

export ORCA_DEV_PODMAN_BIN="$(command -v podman)"
export ORCA_DEV_PODMAN_ROOT="$state/podman-storage"
export ORCA_DEV_PODMAN_TMP="$state/podman-tmp"
export ORCA_CONTAINER_CLI="$repo/scripts/dev-container/podman"
mkdir -p "$ORCA_DEV_PODMAN_ROOT"
mkdir -p "$ORCA_DEV_PODMAN_TMP"
chmod 700 "$ORCA_DEV_PODMAN_ROOT" "$ORCA_DEV_PODMAN_TMP"
podman() { "$ORCA_CONTAINER_CLI" "$@"; }

builder_key=$(cat "$repo/build_linux.sh" "$repo/scripts/linux.d/debian" | sha256sum | cut -c1-12)
export ORCA_DOCKER_IMAGE="localhost/orcaslicer-dev-builder:$builder_key"
export ORCA_DOCKER_BASE_IMAGE=docker.io/library/ubuntu:24.04
desktop_key=$(cat "$repo/scripts/dev-container/Containerfile" "$repo/scripts/dev-container/desktop.sh" \
    "$repo/build_linux.sh" "$repo/scripts/linux.d/debian" | sha256sum | cut -c1-12)
desktop_image="localhost/orcaslicer-dev-desktop:$desktop_key"

prepare_workspace() {
    command -v rsync >/dev/null || { echo "Install rsync first." >&2; exit 1; }
    mkdir -p "$workspace"
    # Generated version/localization files belong to this copy. Preserve its
    # build outputs between syncs, but remove source files deleted in the checkout.
    rsync -a --delete \
        --exclude='/.git' --exclude='/.agents' --exclude='/.claude' --exclude='/.serena' \
        --exclude='/.aws' --exclude='/.codex' --exclude='/.env*' \
        --exclude='/build*/' --exclude='/deps/build*/' --exclude='/deps/DL_CACHE/' \
        --exclude='/docs/superpowers/' --exclude='__pycache__/' \
        "$repo/" "$workspace/"
}

set_memory_limit() {
    export ORCA_CONTAINER_MEMORY="${ORCA_DEV_MEMORY:-$(awk '/MemAvailable:/ {printf "%dm", $2 / 2048}' /proc/meminfo)}"
}

build_desktop_image() {
    podman build -t "$desktop_image" --build-arg "BUILDER_IMAGE=$ORCA_DOCKER_IMAGE" \
        -f "$repo/scripts/dev-container/Containerfile" "$repo/scripts/dev-container"
}

case "$command" in
    image|build)
        mkdir -p "$state"
        exec 9>"$state/build.lock"
        flock -n 9 || { echo "Another development build is running." >&2; exit 1; }
        if podman container exists "$container"; then
            echo "Stop the development GUI before updating its binaries." >&2
            exit 1
        fi
        prepare_workspace
        set_memory_limit
        if [[ "$command" == image ]]; then
            bash "$workspace/build_linux.sh" -g -r
        else
            jobs=${ORCA_DEV_JOBS:-2}
            [[ "$jobs" =~ ^[1-9][0-9]*$ ]] || { echo "ORCA_DEV_JOBS must be positive." >&2; exit 2; }
            # A CMake cache does not imply successful installation. Always resume
            # the incremental dependency build, including after interrupted builds.
            options=(-g -dstrlL -j "$jobs")
            bash "$workspace/build_linux.sh" "${options[@]}" "$@" 2>&1 | tee "$state/build.log"
        fi
        build_desktop_image
        ;;
    test)
        [[ -d "$workspace/build/tests" ]] || { echo "Run the development build first." >&2; exit 1; }
        exec 9>"$state/build.lock"
        flock -n 9 || { echo "Another development build is running." >&2; exit 1; }
        set_memory_limit
        podman run --rm --userns=keep-id \
            --memory="$ORCA_CONTAINER_MEMORY" --memory-swap="$ORCA_CONTAINER_MEMORY" \
            -v "$workspace:/__w/OrcaSlicer/OrcaSlicer" -w /__w/OrcaSlicer/OrcaSlicer \
            "$ORCA_DOCKER_IMAGE" ctest --test-dir build/tests -C Release --output-on-failure "$@"
        ;;
    start)
        [[ -x "$workspace/build/package/orca-slicer" && -x "$workspace/build/package/bin/orca-slicer" ]] || {
            echo "No packaged development binary. Run 'build' first." >&2; exit 1;
        }
        exec 9>"$state/build.lock"
        flock -n 9 || { echo "A development build is running. Wait before starting the GUI." >&2; exit 1; }
        if podman container exists "$container"; then
            echo "A development container already exists. Check 'status' or run 'stop'." >&2
            exit 1
        fi
        podman image exists "$desktop_image" || { echo "Run 'image' to prepare the desktop image." >&2; exit 1; }
        mkdir -p "$state/home" "$state/orca" "$state/runtime" "$state/exchange"
        chmod 700 "$state/home" "$state/orca" "$state/runtime"
        set_memory_limit
        podman run -d --name "$container" --userns=keep-id --network=host \
            --memory="$ORCA_CONTAINER_MEMORY" --memory-swap="$ORCA_CONTAINER_MEMORY" \
            --read-only --tmpfs /tmp:rw,mode=1777 --tmpfs /run:rw \
            --cap-drop=all --security-opt=no-new-privileges --stop-timeout=30 \
            -v "$workspace/build/package:/opt/orca:ro" \
            -v "$state/home:/state/home" -v "$state/orca:/state/orca" \
            -v "$state/runtime:/state/runtime" -v "$state/exchange:/exchange" \
            "$desktop_image"
        for ((attempt = 0; attempt < 30; attempt++)); do
            if [[ "$(podman inspect --format '{{.State.Running}}' "$container")" != true ]]; then
                podman logs --tail 20 "$container" >&2
                echo "Development desktop exited. Application log: $state/runtime/orca.log" >&2
                exit 1
            fi
            if podman exec "$container" bash -c '
                [[ -s /state/runtime/app.pid ]] || exit 1
                kill -0 "$(cat /state/runtime/app.pid)" || exit 1
                python3 -c "import urllib.request; urllib.request.urlopen(\"http://127.0.0.1:6080/vnc.html\", timeout=1)"
            ' >/dev/null 2>&1; then
                echo "Desktop: http://127.0.0.1:6080/vnc.html"
                echo "Password file: $state/runtime/desktop-password"
                exit 0
            fi
            sleep 1
        done
        echo "Development desktop did not become ready. Run 'status' and check $state/runtime/." >&2
        exit 1
        ;;
    stop)
        if podman container exists "$container"; then
            podman stop "$container" >/dev/null
            podman rm "$container" >/dev/null
        fi
        echo "Development desktop stopped; state preserved."
        ;;
    status)
        echo "Container: $container"
        echo "Data directory: $state/orca (container: /state/orca)"
        if ! podman container exists "$container"; then echo "Status: stopped"; exit 0; fi
        podman inspect --format 'Status: {{.State.Status}}; exit code: {{.State.ExitCode}}' "$container"
        if [[ "$(podman inspect --format '{{.State.Running}}' "$container")" == true ]]; then
            podman exec "$container" bash -c '
                if [[ -s /state/runtime/app.pid ]]; then
                    pid=$(cat /state/runtime/app.pid)
                    printf "Binary: "; readlink "/proc/$pid/exe"
                    printf "Arguments: "; tr "\0" " " < "/proc/$pid/cmdline"; echo
                else
                    echo "Application is starting."
                fi
            '
        else
            podman logs --tail 15 "$container"
            echo "Application log: $state/runtime/orca.log"
        fi
        ;;
esac
