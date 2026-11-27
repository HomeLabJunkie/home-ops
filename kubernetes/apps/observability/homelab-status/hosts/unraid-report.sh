#!/bin/bash
# Read-only health report for the homelab status page. Runs as the forced
# command of the homelab-status SSH key on unraid; it takes no input and only
# prints tab-separated "key<TAB>value" lines (see hosts/README.md).
set -u
export LC_ALL=C

printf 'host\t%s\n' "$(hostname)"
printf 'uptime_s\t%s\n' "$(cut -d. -f1 /proc/uptime)"
printf 'load\t%s\n' "$(cut -d' ' -f1-3 /proc/loadavg)"
awk '/^(MemTotal|MemAvailable):/ {printf "mem.%s\t%s\n", substr($1, 1, length($1) - 1), $2 * 1024}' /proc/meminfo

# Array state; sbSynced = when the last parity check finished (epoch),
# sbSyncErrs = errors it found, sbSyncExit = its exit code (0 = completed).
for k in mdState mdNumDisabled mdNumInvalid mdNumMissing mdResyncPos mdResyncSize sbSynced sbSyncErrs sbSyncExit; do
    printf 'var.%s\t%s\n' "$k" "$(grep -m1 "^$k=" /var/local/emhttp/var.ini | cut -d= -f2- | tr -d '"')"
done

# One line per disk field: disk.<name>.<field>
awk -F= '
    /^\[/ { n = $0; gsub(/[\[\]"]/, "", n); next }
    /^(device|status|temp|numErrors|type|fsSize|fsUsed|rotational)=/ {
        v = $2; gsub(/"/, "", v); printf "disk.%s.%s\t%s\n", n, $1, v
    }' /var/local/emhttp/disks.ini

df -P -B1 /mnt/user /mnt/cache 2>/dev/null | awk 'NR > 1 {printf "df.%s\t%s %s\n", $6, $2, $3}'

docker ps -a --format 'ctr\t{{.Names}}\t{{.State}}\t{{.Status}}' 2>/dev/null
