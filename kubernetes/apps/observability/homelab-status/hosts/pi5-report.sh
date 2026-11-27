#!/bin/bash
# Read-only health report for the homelab status page. Runs as the forced
# command of the homelab-status SSH key on pi5 (user jeff); it takes no input
# and only prints tab-separated "key<TAB>value" lines (see hosts/README.md).
set -u
export LC_ALL=C

printf 'host\t%s\n' "$(hostname)"
printf 'uptime_s\t%s\n' "$(cut -d. -f1 /proc/uptime)"
printf 'load\t%s\n' "$(cut -d' ' -f1-3 /proc/loadavg)"
awk '/^(MemTotal|MemAvailable):/ {printf "mem.%s\t%s\n", substr($1, 1, length($1) - 1), $2 * 1024}' /proc/meminfo
if [[ -r /sys/class/thermal/thermal_zone0/temp ]]; then
    printf 'cpu_temp_c\t%s\n' "$(awk '{printf "%.1f", $1 / 1000}' /sys/class/thermal/thermal_zone0/temp)"
fi
df -P -B1 / 2>/dev/null | awk 'NR > 1 {printf "df.%s\t%s %s\n", $6, $2, $3}'
docker ps -a --format 'ctr\t{{.Names}}\t{{.State}}\t{{.Status}}' 2>/dev/null
