#!/usr/bin/env bash
# Copyright (c) 2026 Senternal LLC <https://senternaltech.com>
# SPDX-License-Identifier: MIT
#
# capture.sh — drive the tsp143iiilan research capture pack.
#
# Walks 14 scenarios with interactive prompts, runs tcpdump + active probes,
# anonymizes the resulting pcaps, generates text transcripts, and writes per-
# scenario metadata. Designed for a single 60–90 minute focused session with
# a real Star TSP143IIILAN reachable on the LAN.
#
# Usage:
#   ./capture.sh <iface> <printer-ip>          # all scenarios
#   ./capture.sh <iface> <printer-ip> --scenario NAME
#   ./capture.sh <iface> <printer-ip> --dry-run
#
# Outputs:
#   research/raw-unscrubbed/<scenario>/   (gitignored — pre-anonymization)
#   research/<scenario>/                  (committable — anonymized)
#
# Requires: tcpdump, python3. Optional: tshark, nmap, wget, avahi-browse / dns-sd.

set -euo pipefail

# ----- args -----------------------------------------------------------------

usage() {
    sed -n '2,17p' "$0"
    exit "${1:-1}"
}

[[ $# -ge 2 ]] || usage
IFACE=$1
PRINTER_IP=$2
shift 2

ONE_SCENARIO=""
DRY_RUN=0
while [[ $# -gt 0 ]]; do
    case $1 in
        --scenario) ONE_SCENARIO=$2; shift 2 ;;
        --dry-run)  DRY_RUN=1; shift ;;
        -h|--help)  usage 0 ;;
        *) echo "unknown arg: $1" >&2; usage ;;
    esac
done

HERE=$(cd "$(dirname "$0")" && pwd)
RAW_DIR="$HERE/raw-unscrubbed"
OUT_DIR="$HERE"
PROBE="$HERE/probe.py"
ANON="$HERE/anonymize.py"

[[ $EUID -eq 0 ]] && SUDO="" || SUDO="sudo"
HAVE_TSHARK=$(command -v tshark >/dev/null && echo 1 || echo 0)

# ----- detect host + printer L2/L3 ------------------------------------------

detect_host_ip() {
    if command -v ip >/dev/null; then
        ip -o -4 addr show "$IFACE" 2>/dev/null | awk '{print $4}' | cut -d/ -f1 | head -1
    else
        ifconfig "$IFACE" 2>/dev/null | awk '/inet /{print $2; exit}'
    fi
}
detect_host_mac() {
    if command -v ip >/dev/null; then
        ip link show "$IFACE" 2>/dev/null | awk '/ether/{print $2; exit}'
    else
        ifconfig "$IFACE" 2>/dev/null | awk '/ether /{print $2; exit}'
    fi
}
detect_gateway() {
    if command -v ip >/dev/null; then
        ip -4 route show default 2>/dev/null | awk '{print $3; exit}'
    else
        route -n get default 2>/dev/null | awk '/gateway:/{print $2; exit}'
    fi
}
prime_arp() {
    # Prime the kernel's ARP/neigh cache for $1. TCP probe first because it
    # punches through ICMP-blocking firewalls and is portable; ICMP fallback.
    if command -v nc >/dev/null; then
        nc -z -w 1 "$1" 9100 2>/dev/null \
            || nc -z -w 1 "$1" 80   2>/dev/null \
            || nc -z -w 1 "$1" 22   2>/dev/null \
            || true
    fi
    # macOS `ping -W` is milliseconds; Linux `ping -W` is seconds.
    if [[ "$(uname)" == "Darwin" ]]; then
        ping -c 1 -W 1000 "$1" >/dev/null 2>&1 || true
    else
        ping -c 1 -W 1    "$1" >/dev/null 2>&1 || true
    fi
}

normalize_mac() {
    # Pad single-digit bytes (macOS arp prints `0:11:62:...`) to 2-digit form.
    local in=$1 out="" b
    local IFS=:
    for b in $in; do
        out+="${out:+:}$(printf '%02x' "$((0x$b))")"
    done
    printf '%s' "$out"
}

detect_printer_mac() {
    prime_arp "$PRINTER_IP"
    local raw=""
    if command -v ip >/dev/null; then
        raw=$(ip neigh show "$PRINTER_IP" 2>/dev/null \
            | grep -oE '[0-9a-fA-F]{1,2}(:[0-9a-fA-F]{1,2}){5}' | head -1)
    fi
    if [[ -z "$raw" ]]; then
        raw=$(arp -n "$PRINTER_IP" 2>/dev/null \
            | grep -oE '[0-9a-fA-F]{1,2}(:[0-9a-fA-F]{1,2}){5}' | head -1)
    fi
    [[ -n "$raw" ]] && normalize_mac "$raw" || true
}

HOST_IP=$(detect_host_ip || true)
HOST_MAC=$(detect_host_mac || true)
GATEWAY=$(detect_gateway || true)
PRINTER_MAC=$(detect_printer_mac || true)

if [[ $DRY_RUN -eq 0 ]]; then
    [[ -n "$HOST_IP" && -n "$HOST_MAC" ]] || {
        echo "could not detect host IP/MAC for iface '$IFACE'" >&2; exit 2; }
    [[ -n "$PRINTER_MAC" ]] || {
        echo "could not ARP printer at $PRINTER_IP — is it powered and reachable?" >&2; exit 2; }
fi

echo "iface=$IFACE"
echo "host:    $HOST_IP / $HOST_MAC"
echo "printer: $PRINTER_IP / ${PRINTER_MAC:-?}"
echo "gateway: ${GATEWAY:-?}"
echo "tshark:  $([[ $HAVE_TSHARK -eq 1 ]] && echo yes || echo no — text-decode fallback only)"
[[ $DRY_RUN -eq 1 ]] && { echo "(dry run — exiting before any captures)"; exit 0; }

# Cache sudo credentials BEFORE we ever background anything. A backgrounded
# `sudo` cannot read a password from the terminal, so this avoids a hang at
# the first start_tcpdump call.
if [[ -n "$SUDO" ]]; then
    echo "Authenticating sudo for tcpdump (will refresh in background)..."
    sudo -v || { echo "sudo authentication failed" >&2; exit 2; }
    # Refresh the timestamp every 60s while the script runs, in case a
    # scenario takes longer than the sudoers timeout (default 5 min).
    ( while true; do sudo -n -v 2>/dev/null || exit; sleep 60; done ) &
    SUDO_KEEPALIVE_PID=$!
fi

# ----- helpers --------------------------------------------------------------

prompt() {
    local msg=$1
    echo
    echo "--- $msg"
    read -r -p "    Press Enter when ready (or Ctrl-C to abort)... " _
}

start_tcpdump() {
    local pcap=$1; shift
    local filter="$*"
    $SUDO tcpdump -i "$IFACE" -U -w "$pcap" $filter >/dev/null 2>&1 &
    TCPDUMP_PID=$!
    # Give tcpdump a moment to attach BPF before traffic starts.
    sleep 0.7
}

stop_tcpdump() {
    [[ -n "${TCPDUMP_PID:-}" ]] || return 0
    if kill -0 "$TCPDUMP_PID" 2>/dev/null; then
        $SUDO kill -INT "$TCPDUMP_PID" 2>/dev/null || true
        # Brief grace period for tcpdump to flush, then force kill if needed.
        for _ in 1 2 3 4 5 6 7 8 9 10; do
            kill -0 "$TCPDUMP_PID" 2>/dev/null || break
            sleep 0.1
        done
        kill -0 "$TCPDUMP_PID" 2>/dev/null && $SUDO kill -KILL "$TCPDUMP_PID" 2>/dev/null
    fi
    wait "$TCPDUMP_PID" 2>/dev/null || true
    TCPDUMP_PID=""
}

cleanup() {
    stop_tcpdump 2>/dev/null || true
    [[ -n "${SUDO_KEEPALIVE_PID:-}" ]] && kill "$SUDO_KEEPALIVE_PID" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

anon_args=(
    --printer-mac "$PRINTER_MAC" --host-mac "$HOST_MAC"
    --printer-ip  "$PRINTER_IP" --host-ip  "$HOST_IP"
)
[[ -n "$GATEWAY" ]] && anon_args+=(--gateway-ip "$GATEWAY")

anonymize_into() {
    local raw=$1 out=$2
    if [[ ! -s "$raw" ]]; then
        echo "  (no packets captured)"
    else
        python3 "$ANON" "$raw" "$out/capture.pcap" "${anon_args[@]}"
        $SUDO tcpdump -nXXr "$out/capture.pcap" 2>/dev/null > "$out/capture.pcap.txt" || true
        if [[ $HAVE_TSHARK -eq 1 ]]; then
            tshark -V -r "$out/capture.pcap" 2>/dev/null > "$out/capture.decoded.txt" || true
        fi
    fi
    # Anonymize any companion text files that probes wrote pre-anonymization
    # (probe.log, frames.log, etc.). Defense-in-depth on capture.pcap.txt too.
    local text_files=("$out/capture.pcap.txt")
    [[ -f "$out/capture.decoded.txt" ]] && text_files+=("$out/capture.decoded.txt")
    [[ -f "$out/probe.log" ]]          && text_files+=("$out/probe.log")
    [[ -f "$out/frames.log" ]]         && text_files+=("$out/frames.log")
    if (( ${#EXTRA_TEXT[@]} > 0 )); then
        for extra in "${EXTRA_TEXT[@]}"; do
            [[ -f "$extra" ]] && text_files+=("$extra")
        done
    fi
    local rewrite_args=()
    for f in "${text_files[@]}"; do rewrite_args+=(--also-rewrite "$f"); done
    python3 "$ANON" --text-only "${anon_args[@]}" "${rewrite_args[@]}" >/dev/null
}

write_meta() {
    local out=$1 name=$2
    {
        echo "scenario: $name"
        echo "captured: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
        echo "host_os:  $(uname -sr)"
        echo "iface:    $IFACE"
        echo "tshark:   $([[ $HAVE_TSHARK -eq 1 ]] && echo yes || echo no)"
        echo
        echo "Substitutions applied (originals not retained):"
        echo "  printer MAC -> 02:00:00:00:00:01"
        echo "  host MAC    -> 02:00:00:00:00:02"
        echo "  printer IP  -> 192.0.2.10"
        echo "  host IP     -> 192.0.2.20"
        echo "  gateway IP  -> 192.0.2.1"
    } > "$out/meta.txt"
}

run_scenario() {
    local name=$1
    if [[ -n "$ONE_SCENARIO" && "$ONE_SCENARIO" != "$name" ]]; then
        return 0
    fi
    echo
    echo "============================================================"
    echo "  scenario: $name"
    echo "============================================================"
    SCEN_RAW="$RAW_DIR/$name"
    SCEN_OUT="$OUT_DIR/$name"
    mkdir -p "$SCEN_RAW" "$SCEN_OUT"
    EXTRA_TEXT=()
    "scen_$name"
    write_meta "$SCEN_OUT" "$name"
    echo "  -> wrote $SCEN_OUT/"
}

# ----- scenario implementations --------------------------------------------

scen_boot() {
    prompt "Power the printer OFF. Capture starts now; power it ON when prompted."
    start_tcpdump "$SCEN_RAW/capture.pcap" "host $PRINTER_IP or arp or udp port 67 or udp port 68 or udp port 5353"
    echo "    Capturing — power the printer ON now. Capture runs 90s."
    sleep 90
    stop_tcpdump
    anonymize_into "$SCEN_RAW/capture.pcap" "$SCEN_OUT"
}

scen_idle-long() {
    prompt "Printer is powered, no client connected. 10-minute idle capture."
    start_tcpdump "$SCEN_RAW/capture.pcap" "host $PRINTER_IP"
    sleep 600
    stop_tcpdump
    anonymize_into "$SCEN_RAW/capture.pcap" "$SCEN_OUT"
}

scen_discovery() {
    prompt "Discovery probe to UDP/22222. Will broadcast STR_BCAST + capture reply."
    start_tcpdump "$SCEN_RAW/capture.pcap" "udp port 22222"
    python3 "$PROBE" discovery "$PRINTER_IP" --out "$SCEN_OUT" --hold 3 \
        > "$SCEN_OUT/probe.log" 2>&1 || true
    EXTRA_TEXT=()
    sleep 1
    stop_tcpdump
    anonymize_into "$SCEN_RAW/capture.pcap" "$SCEN_OUT"
}

scen_mdns() {
    prompt "mDNS service dump. Capture is short — service queries are bursty."
    start_tcpdump "$SCEN_RAW/capture.pcap" "udp port 5353"
    if command -v avahi-browse >/dev/null; then
        timeout 10 avahi-browse -art > "$SCEN_OUT/probe.log" 2>&1 || true
    elif command -v dns-sd >/dev/null; then
        ( dns-sd -B _services._dns-sd._udp & sleep 8; kill %1 2>/dev/null ) \
            > "$SCEN_OUT/probe.log" 2>&1 || true
    else
        echo "no avahi-browse or dns-sd available" > "$SCEN_OUT/probe.log"
    fi
    EXTRA_TEXT=()
    stop_tcpdump
    anonymize_into "$SCEN_RAW/capture.pcap" "$SCEN_OUT"
}

scen_portscan() {
    prompt "Full TCP port scan + service fingerprint. ~3-5 min depending on link."
    start_tcpdump "$SCEN_RAW/capture.pcap" "host $PRINTER_IP"
    if command -v nmap >/dev/null; then
        {
            echo "### nmap -sS -sV -p 1-65535"
            $SUDO nmap -sS -sV -p 1-65535 --max-retries 2 "$PRINTER_IP" 2>&1
            echo
            echo "### nmap -sU --top-ports 100"
            $SUDO nmap -sU --top-ports 100 "$PRINTER_IP" 2>&1
            echo
            echo "### snmp scripts"
            nmap --script snmp-info,snmp-sysdescr -p 161 -sU "$PRINTER_IP" 2>&1
        } > "$SCEN_OUT/probe.log"
    else
        echo "nmap not installed; install with brew/apt." > "$SCEN_OUT/probe.log"
    fi
    EXTRA_TEXT=()
    stop_tcpdump
    anonymize_into "$SCEN_RAW/capture.pcap" "$SCEN_OUT"
}

scen_webadmin() {
    prompt "Mirror the printer's web admin pages. Note any login, then take screenshots manually."
    start_tcpdump "$SCEN_RAW/capture.pcap" "host $PRINTER_IP and tcp"
    mkdir -p "$SCEN_OUT/wget"
    if command -v wget >/dev/null; then
        ( cd "$SCEN_OUT/wget" && wget -mk -np -q "http://$PRINTER_IP/" ) \
            2> "$SCEN_OUT/probe.log" || true
    else
        curl -sS "http://$PRINTER_IP/" > "$SCEN_OUT/wget/index.html" 2>"$SCEN_OUT/probe.log" || true
    fi
    EXTRA_TEXT=()
    sleep 2
    stop_tcpdump
    anonymize_into "$SCEN_RAW/capture.pcap" "$SCEN_OUT"
    echo "    NOTE: contents of $SCEN_OUT/wget/ are NOT auto-anonymized — review manually."
}

scen_print-simple() {
    prompt "Minimal print job (init + 2 ETB blocks + cut)."
    start_tcpdump "$SCEN_RAW/capture.pcap" "host $PRINTER_IP and (tcp port 9100 or tcp port 9101)"
    python3 "$PROBE" simple "$PRINTER_IP" --port 9100 --out "$SCEN_OUT" --hold 5 \
        || true
    EXTRA_TEXT=()
    stop_tcpdump
    anonymize_into "$SCEN_RAW/capture.pcap" "$SCEN_OUT"
}

scen_print-drawer() {
    prompt "Print + cash drawer kick. Connect drawer to RJ12 first if not already."
    start_tcpdump "$SCEN_RAW/capture.pcap" "host $PRINTER_IP and (tcp port 9100 or tcp port 9101)"
    python3 "$PROBE" drawer "$PRINTER_IP" --port 9100 --out "$SCEN_OUT" --hold 5 \
        || true
    EXTRA_TEXT=()
    stop_tcpdump
    anonymize_into "$SCEN_RAW/capture.pcap" "$SCEN_OUT"
}

scen_print-large-raster() {
    prompt "Send a multi-KB raster image. Capture flow control / windowing behavior."
    start_tcpdump "$SCEN_RAW/capture.pcap" "host $PRINTER_IP and (tcp port 9100 or tcp port 9101)"
    python3 "$PROBE" raster "$PRINTER_IP" --port 9100 --out "$SCEN_OUT" --hold 8 --rows 800 \
        || true
    EXTRA_TEXT=()
    stop_tcpdump
    anonymize_into "$SCEN_RAW/capture.pcap" "$SCEN_OUT"
}

scen_cover-open() {
    prompt "Will start a print job. OPEN THE COVER mid-job (within ~3s of seeing job start)."
    start_tcpdump "$SCEN_RAW/capture.pcap" "host $PRINTER_IP and (tcp port 9100 or tcp port 9101)"
    python3 "$PROBE" simple "$PRINTER_IP" --port 9100 --out "$SCEN_OUT" --hold 12 \
        || true
    echo "    Now CLOSE the cover. Capture continues for a few more seconds."
    sleep 5
    EXTRA_TEXT=()
    stop_tcpdump
    anonymize_into "$SCEN_RAW/capture.pcap" "$SCEN_OUT"
}

scen_paper-out() {
    prompt "Will start a print job. PULL the paper roll out mid-job. Re-load when prompted."
    start_tcpdump "$SCEN_RAW/capture.pcap" "host $PRINTER_IP and (tcp port 9100 or tcp port 9101)"
    python3 "$PROBE" simple "$PRINTER_IP" --port 9100 --out "$SCEN_OUT" --hold 15 \
        || true
    echo "    Re-load paper. Capture continues for a few more seconds."
    sleep 5
    EXTRA_TEXT=()
    stop_tcpdump
    anonymize_into "$SCEN_RAW/capture.pcap" "$SCEN_OUT"
}

scen_9101-lifecycle() {
    prompt "Hold port 9101 idle for 60s. Will not send any data — just observe what the printer does."
    start_tcpdump "$SCEN_RAW/capture.pcap" "host $PRINTER_IP and tcp port 9101"
    python3 "$PROBE" hold-9101 "$PRINTER_IP" --port 9101 --out "$SCEN_OUT" --hold 60 \
        || true
    EXTRA_TEXT=()
    stop_tcpdump
    anonymize_into "$SCEN_RAW/capture.pcap" "$SCEN_OUT"
}

scen_multi-client() {
    prompt "Two simultaneous TCP/9100 sessions. Watch for refusal vs. interleave."
    start_tcpdump "$SCEN_RAW/capture.pcap" "host $PRINTER_IP and tcp port 9100"
    mkdir -p "$SCEN_OUT/client-a" "$SCEN_OUT/client-b"
    ( python3 "$PROBE" simple "$PRINTER_IP" --port 9100 --out "$SCEN_OUT/client-a" --hold 8 || true ) &
    P1=$!
    sleep 0.2
    ( python3 "$PROBE" simple "$PRINTER_IP" --port 9100 --out "$SCEN_OUT/client-b" --hold 8 || true ) &
    P2=$!
    wait "$P1" "$P2" || true
    EXTRA_TEXT=(
        "$SCEN_OUT/client-a/probe.log" "$SCEN_OUT/client-a/frames.log"
        "$SCEN_OUT/client-b/probe.log" "$SCEN_OUT/client-b/frames.log"
    )
    stop_tcpdump
    anonymize_into "$SCEN_RAW/capture.pcap" "$SCEN_OUT"
}

scen_asb-states() {
    prompt "Manual ASB state walk. Will hold port 9100 with ASB enabled for 90s."
    echo "    During the capture: open cover → close → pull paper → reload → kick drawer → press feed."
    start_tcpdump "$SCEN_RAW/capture.pcap" "host $PRINTER_IP and tcp port 9100"
    python3 "$PROBE" hold-9101 "$PRINTER_IP" --port 9100 --out "$SCEN_OUT" --hold 90 \
        || true
    EXTRA_TEXT=()
    stop_tcpdump
    anonymize_into "$SCEN_RAW/capture.pcap" "$SCEN_OUT"
}

# ----- main -----------------------------------------------------------------

mkdir -p "$RAW_DIR"

SCENARIOS=(
    boot
    idle-long
    discovery
    mdns
    portscan
    webadmin
    print-simple
    print-drawer
    print-large-raster
    cover-open
    paper-out
    9101-lifecycle
    multi-client
    asb-states
)

for s in "${SCENARIOS[@]}"; do
    run_scenario "$s"
done

echo
echo "============================================================"
echo "  All scenarios complete."
echo "  Anonymized output: $OUT_DIR/*/"
echo "  Raw quarantine:    $RAW_DIR/   (gitignored — review then delete)"
echo
echo "  Next steps:"
echo "    1. Hand-write research/notes-*.md cross-cutting analyses."
echo "    2. Run the verification checklist in research/README.md."
echo "    3. Delete or archive research/raw-unscrubbed/ before commit."
echo "============================================================"
