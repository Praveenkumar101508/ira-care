#!/usr/bin/env bash
# =============================================================================
# verify-native-install.sh — run the documented install on THIS machine and
# write down what happened, in a folder you can send back. Linux and macOS;
# written for bash 3.2 (macOS).
#
# Usage (from supracloud-jarvis/, after INSTALL.md's "install the prerequisites"):
#   bash scripts/verify-native-install.sh                 # report in ~/ira-install-check-<time>/
#   bash scripts/verify-native-install.sh --out DIR
#   bash scripts/verify-native-install.sh --print-listeners   # only list IRA's listening ports
#
# Steps, each recorded PASS / FAIL with its log:
#   machine   OS, chip, CPU count, memory (no host name, no user name)
#   setup     bash scripts/setup-native.sh           (safe to re-run: keeps .env and data)
#   start     ./start-ira.sh, exactly as INSTALL.md says
#   loopback  IRA listens on 127.0.0.1 only
#   smoke     scripts/smoke_test.py: sign-in, real chat, memory, recall, approvals
#   stop      ./stop-ira.sh, then nothing left listening
#   uninstall uninstall-native.sh as a DRY RUN only: nothing is removed
#
# If ./start-ira.sh itself fails, that is recorded as a FAIL and the rest runs
# with `bash start-ira.sh`, so one defect does not hide the others.
#
# The report holds no password and no .env: logs are scanned and any
# "PASSWORD=" or "SECRET" value is replaced before they are written, and your
# home folder is shown as "~".
# =============================================================================
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="$HOME/ira-install-check-$(date -u +%Y%m%dT%H%M%SZ)"
PRINT_LISTENERS=0
while [ $# -gt 0 ]; do
    case "$1" in
        --out) [ $# -ge 2 ] || { echo "--out needs a folder" >&2; exit 2; }; OUT="$2"; shift 2 ;;
        --print-listeners) PRINT_LISTENERS=1; shift ;;
        -h|--help) sed -n '2,28p' "$0"; exit 0 ;;
        *) echo "unknown option: $1" >&2; exit 2 ;;
    esac
done
[ "$PRINT_LISTENERS" = 1 ] || mkdir -p "$OUT" || { echo "cannot create $OUT" >&2; exit 1; }
SUMMARY="$OUT/SUMMARY.txt"
OS="$(uname -s)"
FAILS=0

clean() {  # clean <file>: strip colours, hide the home folder, blank secret values
    sed -E 's/\x1b\[[0-9;]*m//g' "$1" \
      | sed "s#$HOME#~#g" \
      | sed -E 's/((PASSWORD|SECRET|TOKEN|KEY)[A-Z_]*=)[^[:space:]]+/\1<redacted>/g' > "$1.tmp" \
      && mv "$1.tmp" "$1"
}
record() {  # record <step> <PASS|FAIL> <detail>
    printf '%-9s %-4s %s\n' "$1" "$2" "$3" | tee -a "$SUMMARY"
    [ "$2" = PASS ] || FAILS=$((FAILS + 1))
}
# IRA's ports, and where Linux keeps its socket tables. Both variables exist only so this
# script's own tests can point them at a free port and at synthetic tables.
PORTS="${IRA_VERIFY_PORTS:-3000 8000}"
PROC_NET="${IRA_VERIFY_PROC_NET:-/proc/net}"
PORT_RE=":($(printf '%s\n' $PORTS | paste -sd'|' -))\$"
proc_listeners() {  # Linux with neither ss nor lsof: read the kernel's own socket tables
    # Each row's local address is hex: IPv4 as 4 little-endian bytes, IPv6 as 4 little-endian
    # 32-bit words. State 0A is LISTEN. Written for mawk, which has no strtonum().
    awk 'function hex(s,   i, n) { n = 0; s = toupper(s)
             for (i = 1; i <= length(s); i++) n = n * 16 + index("0123456789ABCDEF", substr(s, i, 1)) - 1
             return n }
         function v4(h) { return hex(substr(h,7,2)) "." hex(substr(h,5,2)) "." hex(substr(h,3,2)) "." hex(substr(h,1,2)) }
         FNR > 1 && $4 == "0A" {
             split($2, a, ":"); h = a[1]; port = hex(a[2])
             if (length(h) == 8) { print v4(h) ":" port; next }
             b = ""; for (w = 0; w < 4; w++) { x = substr(h, w * 8 + 1, 8)
                 b = b substr(x,7,2) substr(x,5,2) substr(x,3,2) substr(x,1,2) }
             b = tolower(b)
             if (b == "00000000000000000000000000000001") addr = "[::1]"
             else if (b == "00000000000000000000000000000000") addr = "[::]"
             else if (substr(b, 1, 24) == "00000000000000000000ffff") {
                 y = substr(b, 25, 8); addr = hex(substr(y,1,2)) "." hex(substr(y,3,2)) "." hex(substr(y,5,2)) "." hex(substr(y,7,2)) }
             else { addr = "["; for (g = 0; g < 8; g++) addr = addr (g ? ":" : "") substr(b, g * 4 + 1, 4); addr = addr "]" }
             print addr ":" port }' "$@"
}
listeners() {  # the IRA ports that are listening, one "address:port" per line
    # Exit 3 means the sockets could not be listed at all, which is not the same as none
    # listening. This machine once reported "nothing is listening" with IRA up, because
    # `ss` was not installed and its error was hidden.
    # On Linux the kernel's own views come first. `lsof` lists only sockets it can tie to
    # a process it can parse, and lsof 4.95 skips a process whose name holds an unclosed
    # "(". The frontend runs as "next-server (v16.4.1)", which the kernel cuts to 15
    # characters, "next-server (v1", so lsof never sees port 3000.
    if [ "$OS" = Darwin ]; then
        command -v lsof >/dev/null 2>&1 || return 3
        lsof -nP -iTCP -sTCP:LISTEN 2>/dev/null | awk 'NR>1 {print $9}' | grep -E "$PORT_RE" | sort -u
    elif command -v ss >/dev/null 2>&1; then
        ss -ltnH 2>/dev/null | awk '{print $4}' | grep -E "$PORT_RE" | sort -u
    elif [ -r "$PROC_NET/tcp" ]; then
        set -- "$PROC_NET/tcp"; [ -r "$PROC_NET/tcp6" ] && set -- "$@" "$PROC_NET/tcp6"
        proc_listeners "$@" | grep -E "$PORT_RE" | sort -u
    elif command -v lsof >/dev/null 2>&1; then
        lsof -nP -iTCP -sTCP:LISTEN 2>/dev/null | awk 'NR>1 {print $9}' | grep -E "$PORT_RE" | sort -u
    else
        return 3
    fi
    return 0
}
if [ "$PRINT_LISTENERS" = 1 ]; then listeners; exit $?; fi
RC=0; SECS=0
timed() {  # timed <log> <command...>: run it, keep its output, set RC and SECS
    local log="$1"; shift
    local t0; t0="$(date +%s)"
    "$@" > "$log" 2>&1
    RC=$?
    SECS=$(( $(date +%s) - t0 ))
    clean "$log"
}

cd "$ROOT" || exit 1
echo "IRA install check — commit $(git rev-parse --short HEAD 2>/dev/null || echo unknown)" | tee "$SUMMARY"

# machine
if [ "$OS" = Darwin ]; then
    chip="$(sysctl -n machdep.cpu.brand_string 2>/dev/null)"
    mem_gib="$(( $(sysctl -n hw.memsize 2>/dev/null || echo 0) / 1073741824 ))"
    osver="macOS $(sw_vers -productVersion 2>/dev/null)"
else
    chip="$(awk -F': ' '/model name/ {print $2; exit}' /proc/cpuinfo 2>/dev/null)"
    mem_gib="$(awk '/MemTotal/ {printf "%d", $2/1048576}' /proc/meminfo 2>/dev/null)"
    osver="$(. /etc/os-release 2>/dev/null && echo "$PRETTY_NAME")"
fi
cpus="$(getconf _NPROCESSORS_ONLN 2>/dev/null || echo ?)"
record machine PASS "$osver, $(uname -m), ${chip:-unknown chip}, $cpus CPUs, ${mem_gib} GiB"

# setup
timed "$OUT/setup.log" bash scripts/setup-native.sh
if [ "$RC" = 0 ]; then record setup PASS "exit 0 in $SECS s"; else record setup FAIL "exit $RC in $SECS s (see setup.log)"; fi

# start, exactly as documented
timed "$OUT/start.log" ./start-ira.sh
if [ "$RC" = 0 ]; then
    record start PASS "./start-ira.sh exit 0 in $SECS s"
else
    record start FAIL "./start-ira.sh exit $RC (see start.log); continuing with bash start-ira.sh"
    timed "$OUT/start-with-bash.log" bash start-ira.sh
    [ "$RC" = 0 ] || record start FAIL "bash start-ira.sh exit $RC too (see start-with-bash.log)"
fi

# loopback only
sleep 3
found="$(listeners)"; lrc=$?
if [ "$lrc" = 3 ]; then
    record loopback FAIL "cannot list listening sockets here (no ss, $PROC_NET/tcp or lsof), so this is unchecked"
elif [ -z "$found" ]; then
    record loopback FAIL "nothing is listening on 3000 or 8000"
elif printf '%s\n' "$found" | grep -qvE '^(127\.0\.0\.1|\[::1\]|::1|localhost):'; then
    record loopback FAIL "listening beyond this machine: $(printf '%s ' $found)"
else
    record loopback PASS "$(printf '%s ' $found)"
fi

# smoke test
py="$ROOT/.venv/bin/python"; [ -x "$py" ] || py=python3
timed "$OUT/smoke.log" "$py" scripts/smoke_test.py --out "$OUT/smoke-test.json"
verdict="$(grep -E '^(PASS|FAIL):' "$OUT/smoke.log" | tail -1)"
if [ "$RC" = 0 ]; then record smoke PASS "$verdict"; else record smoke FAIL "${verdict:-no verdict} (see smoke.log)"; fi

# stop, exactly as documented
timed "$OUT/stop.log" ./stop-ira.sh
stop_rc="$RC"
if [ "$stop_rc" != 0 ]; then
    record stop FAIL "./stop-ira.sh exit $stop_rc (see stop.log); stopping with bash stop-ira.sh"
    bash stop-ira.sh >/dev/null 2>&1
fi
sleep 2
left="$(listeners)"; lrc=$?
if [ "$lrc" = 3 ]; then
    record stop FAIL "cannot list listening sockets here, so 'nothing left listening' is unchecked"
elif [ -n "$left" ]; then
    record stop FAIL "still listening after stop: $(printf '%s ' $left)"
elif [ "$stop_rc" = 0 ]; then
    record stop PASS "./stop-ira.sh exit 0; nothing left listening"
fi

# uninstall, dry run only
if [ -f scripts/uninstall-native.sh ]; then
    timed "$OUT/uninstall-dry-run.log" bash scripts/uninstall-native.sh
    if [ "$RC" = 0 ]; then record uninstall PASS "dry run listed its plan; nothing removed"
    else record uninstall FAIL "dry run exit $RC (see uninstall-dry-run.log)"; fi
fi

echo "" | tee -a "$SUMMARY"
if [ "$FAILS" -eq 0 ]; then echo "ALL PASS" | tee -a "$SUMMARY"; else echo "$FAILS step(s) FAILED" | tee -a "$SUMMARY"; fi
echo "Report: $OUT  (no passwords; send the whole folder)"
[ "$FAILS" -eq 0 ]
