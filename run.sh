#!/bin/bash
# Copyright (c) 2026 Senternal LLC <https://senternaltech.com>
# SPDX-License-Identifier: MIT
#
# Launches the StarIO TCP-to-USB bridge and Star printer discovery responder.
# Kills all children when any exits or the script receives SIGTERM.

DIR="$(cd "$(dirname "$0")" && pwd)"

cleanup() {
    kill "$BRIDGE_PID" "$DISCOVERY_PID" 2>/dev/null
    wait
}
trap cleanup EXIT SIGTERM SIGINT

python3 "$DIR/print_server_bidir.py" &
BRIDGE_PID=$!

python3 "$DIR/star_discovery.py" -v &
DISCOVERY_PID=$!

# Wait for either to exit — then cleanup kills the other
wait -n
