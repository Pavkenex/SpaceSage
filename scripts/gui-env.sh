#!/usr/bin/env sh
# Source this file (`. scripts/gui-env.sh`) before running GUI code or tests
# in the dev container. No-op on normal machines with system GL libraries.
SYS_LIB=/opt/data/syslibs/usr/lib/aarch64-linux-gnu
if [ -d "$SYS_LIB" ]; then
    case ":$LD_LIBRARY_PATH:" in
        *":$SYS_LIB:"*) ;;
        *) export LD_LIBRARY_PATH="$SYS_LIB${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}" ;;
    esac
fi
if [ -z "${DISPLAY:-}" ] && [ -z "${WAYLAND_DISPLAY:-}" ]; then
    export QT_QPA_PLATFORM="${QT_QPA_PLATFORM:-offscreen}"
fi
