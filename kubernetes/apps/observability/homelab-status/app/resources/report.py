#!/usr/bin/env python3
"""Daily homelab health report.

Collects the state of the prodesk cluster (Kubernetes API, Prometheus,
Alertmanager), unraid and pi5 (read-only report scripts over a restricted SSH
key) and a set of HTTP probes, then writes a static HTML page plus JSON into
the ConfigMap that the homelab-status nginx serves at status.jeffriffle.com.

Standard library only. Every collector is isolated: one that fails shows up as
an "unknown" section on the page instead of breaking the run.
"""

import datetime as dt
import html
import http.client
import json
import os
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import zoneinfo

TZ = zoneinfo.ZoneInfo("America/Chicago")
NOW = dt.datetime.now(dt.timezone.utc)
NAMESPACE = os.environ.get("PAGE_NAMESPACE", "observability")
PAGE_CONFIGMAP = os.environ.get("PAGE_CONFIGMAP", "homelab-status-page")
CONFIG_DIR = os.environ.get("CONFIG_DIR", "/etc/homelab-status/config")
SSH_KEY = os.environ.get("SSH_KEY", "/etc/homelab-status/ssh/id_ed25519")
PROMETHEUS = os.environ.get("PROMETHEUS_URL", "http://prometheus-operated.observability.svc.cluster.local:9090")
ALERTMANAGER = os.environ.get("ALERTMANAGER_URL", "http://alertmanager-operated.observability.svc.cluster.local:9093")
HISTORY_DAYS = 30

OK, WARN, CRIT, UNKNOWN = "ok", "warn", "crit", "unknown"
RANK = {OK: 0, UNKNOWN: 1, WARN: 2, CRIT: 3}
LABEL = {OK: "Healthy", WARN: "Warning", CRIT: "Critical", UNKNOWN: "Unknown"}


def worst(*states):
    states = [s for s in states if s]
    return max(states, key=lambda s: RANK[s]) if states else OK


def c_to_f(c):
    return round(float(c) * 9 / 5 + 32)


def human_bytes(n):
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if abs(n) < 1024 or unit == "PB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def human_age(seconds):
    seconds = int(seconds)
    if seconds < 90:
        return f"{seconds}s"
    if seconds < 5400:
        return f"{seconds // 60}m"
    if seconds < 172800:
        return f"{seconds / 3600:.0f}h"
    return f"{seconds / 86400:.0f}d"


def parse_time(value):
    if not value:
        return None
    value = value.replace("Z", "+00:00")
    # Some controllers write nanoseconds; Python only takes microseconds.
    if "." in value:
        head, rest = value.split(".", 1)
        frac = "".join(ch for ch in rest if ch.isdigit())
        tz = rest[len(frac):]
        value = f"{head}.{frac[:6]}{tz}"
    return dt.datetime.fromisoformat(value)


def age_of(value):
    t = parse_time(value)
    return (NOW - t).total_seconds() if t else None


def local(t):
    return t.astimezone(TZ).strftime("%a %b %-d, %-I:%M %p %Z")


# --------------------------------------------------------------- data access


class Kube:
    def __init__(self):
        sa = "/var/run/secrets/kubernetes.io/serviceaccount"
        with open(f"{sa}/token") as f:
            self.token = f.read().strip()
        self.ctx = ssl.create_default_context(cafile=f"{sa}/ca.crt")
        host = os.environ.get("KUBERNETES_SERVICE_HOST", "kubernetes.default.svc")
        port = os.environ.get("KUBERNETES_SERVICE_PORT", "443")
        self.base = f"https://{host}:{port}"

    def request(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method)
        req.add_header("Authorization", f"Bearer {self.token}")
        req.add_header("Accept", "application/json")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        # The API server's priority and fairness can answer 429 (or a brief
        # 5xx); back off and retry instead of failing the section.
        for attempt in range(5):
            try:
                with urllib.request.urlopen(req, context=self.ctx, timeout=30) as resp:
                    return json.load(resp)
            except urllib.error.HTTPError as exc:
                if exc.code not in (429, 500, 502, 503, 504) or attempt == 4:
                    raise
                retry_after = exc.headers.get("Retry-After", "")
                time.sleep(int(retry_after) if retry_after.isdigit() else 2 ** attempt)

    def get(self, path):
        return self.request("GET", path)

    def items(self, group_version, plural, namespace=None):
        prefix = "/api/v1" if group_version == "v1" else f"/apis/{group_version}"
        ns = f"/namespaces/{namespace}" if namespace else ""
        return self.get(f"{prefix}{ns}/{plural}").get("items", [])


def prom(query):
    url = f"{PROMETHEUS}/api/v1/query?" + urllib.parse.urlencode({"query": query})
    with urllib.request.urlopen(url, timeout=30) as resp:
        data = json.load(resp)
    if data.get("status") != "success":
        raise RuntimeError(f"Prometheus query failed: {query}")
    return data["data"]["result"]


def prom_by(query, label):
    return {r["metric"].get(label, ""): float(r["value"][1]) for r in prom(query)}


def condition(obj, ctype="Ready"):
    for c in obj.get("status", {}).get("conditions", []) or []:
        if c.get("type") == ctype:
            return c
    return {}


# --------------------------------------------------------------- sections


class Section:
    def __init__(self, key, title):
        self.key = key
        self.title = title
        self.status = OK
        self.summary = ""
        self.facts = []      # (label, value) pairs shown as tiles
        self.tables = []     # (caption, headers, rows); a row is a list of cells
        self.notes = []      # (status, text) findings

    def flag(self, status, text):
        self.status = worst(self.status, status)
        self.notes.append((status, text))

    def as_dict(self):
        return {
            "key": self.key,
            "title": self.title,
            "status": self.status,
            "summary": self.summary,
            "findings": [{"status": s, "text": t} for s, t in self.notes],
        }


def cell(text, status=None):
    return {"text": str(text), "status": status}


def collect_nodes(kube, sec):
    nodes = kube.items("v1", "nodes")
    cpu = mem = fs = temp = {}
    try:
        uname = {r["metric"]["instance"]: r["metric"]["nodename"] for r in prom("node_uname_info")}

        def by_node(q):
            return {uname.get(k, k): v for k, v in prom_by(q, "instance").items()}

        cpu = by_node('100 * (1 - avg by (instance) (rate(node_cpu_seconds_total{mode="idle"}[30m])))')
        mem = by_node("100 * (1 - node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes)")
        fs = by_node('100 * (1 - node_filesystem_avail_bytes{mountpoint="/var"} / node_filesystem_size_bytes{mountpoint="/var"})')
        temp = by_node("max by (instance) (node_hwmon_temp_celsius)")
    except Exception as exc:  # metrics are a bonus; node state still counts
        sec.flag(WARN, f"Node metrics unavailable: {exc}")

    rows, ready = [], 0
    versions = set()
    for n in sorted(nodes, key=lambda x: x["metadata"]["name"]):
        name = n["metadata"]["name"]
        info = n["status"].get("nodeInfo", {})
        is_ready = condition(n).get("status") == "True"
        cordoned = bool(n["spec"].get("unschedulable"))
        ready += is_ready
        versions.add(info.get("osImage", ""))
        state = OK if is_ready and not cordoned else (WARN if is_ready else CRIT)
        if not is_ready:
            sec.flag(CRIT, f"{name} is NotReady")
        elif cordoned:
            sec.flag(WARN, f"{name} is cordoned")
        role = "control plane" if "node-role.kubernetes.io/control-plane" in n["metadata"].get("labels", {}) else "worker"

        def pct(d, warn_at, crit_at):
            v = d.get(name)
            if v is None:
                return cell("–")
            st = CRIT if v >= crit_at else WARN if v >= warn_at else None
            if st:
                sec.flag(st, f"{name}: {v:.0f}% used")
            return cell(f"{v:.0f}%", st)

        t = temp.get(name)
        t_cell = cell("–")
        if t is not None:
            st = CRIT if t >= 90 else WARN if t >= 80 else None
            if st:
                sec.flag(st, f"{name}: hottest sensor {c_to_f(t)}°F")
            t_cell = cell(f"{c_to_f(t)}°F", st)
        rows.append([
            cell(name),
            cell(role),
            cell("Ready" + (", cordoned" if cordoned else "") if is_ready else "NotReady", state),
            cell(info.get("osImage", "").replace("Talos ", "")),
            cell(info.get("kubeletVersion", "")),
            pct(cpu, 80, 95),
            pct(mem, 85, 95),
            pct(fs, 80, 90),
            t_cell,
        ])
    if len(versions) > 1:
        sec.flag(WARN, "Nodes run different Talos versions: " + ", ".join(sorted(versions)))
    sec.summary = f"{ready}/{len(nodes)} nodes Ready"
    sec.facts = [("Nodes Ready", f"{ready}/{len(nodes)}"), ("Talos", ", ".join(sorted(v.replace('Talos ', '') for v in versions)))]
    sec.tables.append(("Nodes", ["Node", "Role", "State", "Talos", "Kubelet", "CPU (30m)", "Memory", "/var", "Hottest sensor"], rows))

    # tuppr upgrade controllers
    up_rows = []
    for plural, label, path in (("talosupgrades", "Talos", ("talos", "version")), ("kubernetesupgrades", "Kubernetes", ("kubernetes", "version"))):
        try:
            for u in kube.items("tuppr.home-operations.com/v1alpha1", plural):
                phase = u.get("status", {}).get("phase", "")
                target = u.get("spec", {}).get(path[0], {}).get(path[1], "")
                st = OK if phase == "Completed" else CRIT if phase == "Failed" else WARN
                if phase == "Failed":
                    sec.flag(CRIT, f"{label} upgrade failed: {u.get('status', {}).get('message', '')}")
                elif phase != "Completed":
                    sec.flag(WARN, f"{label} upgrade to {target} is {phase or 'pending'}")
                up_rows.append([cell(label), cell(target), cell(phase or "–", st)])
        except urllib.error.HTTPError:
            pass
    if up_rows:
        sec.tables.append(("Upgrades (tuppr)", ["Component", "Target", "Phase"], up_rows))


def collect_flux(kube, sec):
    kinds = (
        ("Kustomization", "kustomize.toolkit.fluxcd.io/v1", "kustomizations"),
        ("HelmRelease", "helm.toolkit.fluxcd.io/v2", "helmreleases"),
    )
    total = bad = suspended = 0
    rows = []
    for kind, gv, plural in kinds:
        for obj in kube.items(gv, plural):
            total += 1
            name = f"{obj['metadata']['namespace']}/{obj['metadata']['name']}"
            if obj.get("spec", {}).get("suspend"):
                suspended += 1
                sec.flag(WARN, f"{kind} {name} is suspended")
                rows.append([cell(kind), cell(name), cell("Suspended", WARN), cell("")])
                continue
            c = condition(obj)
            if c.get("status") != "True":
                bad += 1
                sec.flag(CRIT if c.get("status") == "False" else WARN, f"{kind} {name}: {c.get('reason', 'not ready')}")
                rows.append([cell(kind), cell(name), cell(c.get("reason", "Not ready"), CRIT), cell(c.get("message", "")[:160])])
    rev = ""
    try:
        repo = kube.get("/apis/source.toolkit.fluxcd.io/v1/namespaces/flux-system/gitrepositories/flux-system")
        rev = repo.get("status", {}).get("artifact", {}).get("revision", "")
        rev = rev.split("sha1:")[-1][:8] if rev else ""
    except Exception:
        pass
    sec.summary = f"{total - bad - suspended}/{total} reconciled" + (f", revision {rev}" if rev else "")
    sec.facts = [("Reconciled", f"{total - bad - suspended}/{total}"), ("Git revision", rev or "–")]
    if rows:
        sec.tables.append(("Not ready", ["Kind", "Name", "State", "Message"], rows))


def collect_workloads(kube, sec):
    pods = kube.items("v1", "pods")
    rows = []
    for p in pods:
        phase = p.get("status", {}).get("phase")
        name = f"{p['metadata']['namespace']}/{p['metadata']['name']}"
        if phase in ("Succeeded",):
            continue
        waiting = [
            cs.get("state", {}).get("waiting", {}).get("reason", "")
            for cs in (p.get("status", {}).get("containerStatuses") or [])
            if cs.get("state", {}).get("waiting")
        ]
        not_ready = phase == "Running" and any(not cs.get("ready") for cs in (p["status"].get("containerStatuses") or []))
        if phase != "Running" or waiting or not_ready:
            age = age_of(p["metadata"].get("creationTimestamp")) or 0
            if age < 600 and phase == "Pending":
                continue  # still starting
            reason = ", ".join(w for w in waiting if w) or ("Not ready" if not_ready else phase)
            st = CRIT if any(w in ("CrashLoopBackOff", "ImagePullBackOff", "ErrImagePull") for w in waiting) or phase == "Failed" else WARN
            sec.flag(st, f"{name}: {reason}")
            rows.append([cell(name), cell(reason, st), cell(p["spec"].get("nodeName", "–")), cell(human_age(age))])
    restarts = []
    try:
        for r in prom("sum by (namespace, pod) (increase(kube_pod_container_status_restarts_total[24h])) >= 1"):
            n = round(float(r["value"][1]))
            if n >= 1:
                restarts.append((n, f"{r['metric']['namespace']}/{r['metric']['pod']}"))
    except Exception as exc:
        sec.flag(WARN, f"Restart metrics unavailable: {exc}")
    restarts.sort(reverse=True)
    for n, name in restarts:
        if n >= 5:
            sec.flag(WARN, f"{name} restarted {n} times in 24h")
    running = sum(1 for p in pods if p.get("status", {}).get("phase") == "Running")
    sec.summary = f"{running} pods running, {len(rows)} with problems, {len(restarts)} restarted in 24h"
    sec.facts = [("Running pods", running), ("Problem pods", len(rows)), ("Restarted (24h)", len(restarts))]
    if rows:
        sec.tables.append(("Pods with problems", ["Pod", "Reason", "Node", "Age"], rows))
    if restarts:
        sec.tables.append(("Container restarts in the last 24h", ["Pod", "Restarts"], [[cell(n), cell(c, WARN if c >= 5 else None)] for c, n in restarts[:15]]))


def collect_storage(kube, sec):
    vols = kube.items("longhorn.io/v1beta2", "volumes", "longhorn-system")
    counts = {}
    rows = []
    for v in vols:
        st = v.get("status", {})
        rob = st.get("robustness", "unknown")
        counts[rob] = counts.get(rob, 0) + 1
        pvc = st.get("kubernetesStatus", {}).get("pvcName") or v["metadata"]["name"]
        if rob == "faulted":
            sec.flag(CRIT, f"Volume {pvc} is faulted")
        elif rob == "degraded":
            sec.flag(WARN, f"Volume {pvc} is degraded")
        if rob in ("faulted", "degraded"):
            rows.append([cell(pvc), cell(st.get("state", "")), cell(rob, CRIT if rob == "faulted" else WARN), cell(st.get("currentNodeID") or "–")])
    disk_rows = []
    for n in kube.items("longhorn.io/v1beta2", "nodes", "longhorn-system"):
        for disk in (n.get("status", {}).get("diskStatus") or {}).values():
            mx, av = disk.get("storageMaximum", 0), disk.get("storageAvailable", 0)
            if not mx:
                continue
            used = 100 * (1 - av / mx)
            s = CRIT if used >= 85 else WARN if used >= 70 else None
            if s:
                sec.flag(s, f"Longhorn disk on {n['metadata']['name']} is {used:.0f}% full")
            disk_rows.append([cell(n["metadata"]["name"]), cell(human_bytes(mx - av)), cell(human_bytes(mx)), cell(f"{used:.0f}%", s)])
    pvc_rows = []
    try:
        for r in prom("100 * kubelet_volume_stats_used_bytes / kubelet_volume_stats_capacity_bytes >= 75"):
            v = float(r["value"][1])
            name = f"{r['metric'].get('namespace')}/{r['metric'].get('persistentvolumeclaim')}"
            s = CRIT if v >= 90 else WARN
            sec.flag(s, f"PVC {name} is {v:.0f}% full")
            pvc_rows.append([cell(name), cell(f"{v:.0f}%", s)])
    except Exception as exc:
        sec.flag(WARN, f"PVC metrics unavailable: {exc}")
    healthy = counts.get("healthy", 0) + counts.get("unknown", 0)
    sec.summary = f"{counts.get('healthy', 0)} healthy, {counts.get('degraded', 0)} degraded, {counts.get('faulted', 0)} faulted, {counts.get('unknown', 0)} detached"
    sec.facts = [("Volumes OK", f"{healthy}/{len(vols)}"), ("Degraded", counts.get("degraded", 0)), ("Faulted", counts.get("faulted", 0))]
    if rows:
        sec.tables.append(("Volumes needing attention", ["PVC", "State", "Robustness", "Attached to"], rows))
    if pvc_rows:
        sec.tables.append(("PVCs over 75% full", ["PVC", "Used"], pvc_rows))
    sec.tables.append(("Longhorn disks", ["Node", "Used", "Size", "Used %"], disk_rows))


def collect_backups(kube, sec):
    rows = []
    # kopiur: latest snapshot per schedule
    latest = {}
    for s in kube.items("kopiur.home-operations.com/v1alpha1", "snapshots"):
        key = f"{s['metadata']['namespace']}/{s['metadata'].get('labels', {}).get('kopiur.home-operations.com/schedule', s['metadata']['name'])}"
        latest.setdefault(key, []).append(s)
    for key, snaps in sorted(latest.items()):
        snaps.sort(key=lambda x: x["metadata"]["creationTimestamp"])
        last = snaps[-1]
        ok_snaps = [x for x in snaps if x.get("status", {}).get("phase") == "Succeeded"]
        last_ok = ok_snaps[-1] if ok_snaps else None
        age = age_of(last_ok["metadata"]["creationTimestamp"]) if last_ok else None
        phase = last.get("status", {}).get("phase", "")
        st = OK
        if age is None or age > 26 * 3600:
            st = CRIT
            sec.flag(CRIT, f"kopiur {key}: no successful snapshot in 26h")
        elif age > 3 * 3600:
            st = WARN
            sec.flag(WARN, f"kopiur {key}: last success {human_age(age)} ago")
        if phase == "Failed":
            st = worst(st, WARN)
            sec.flag(WARN, f"kopiur {key}: latest snapshot failed")
        size = (last_ok or {}).get("status", {}).get("stats", {}).get("sizeBytes")
        rows.append([cell("kopiur (S3)"), cell(key), cell(phase or "–", st), cell(f"{human_age(age)} ago" if age is not None else "never", st), cell(human_bytes(size) if size else "–")])
    # CNPG: latest backup per cluster
    try:
        clusters = {}
        for b in kube.items("postgresql.cnpg.io/v1", "backups"):
            key = f"{b['metadata']['namespace']}/{b['spec'].get('cluster', {}).get('name', '?')}"
            clusters.setdefault(key, []).append(b)
        for key, backups in sorted(clusters.items()):
            done = [b for b in backups if b.get("status", {}).get("phase") == "completed"]
            done.sort(key=lambda b: b["status"].get("stoppedAt", ""))
            age = age_of(done[-1]["status"].get("stoppedAt")) if done else None
            st = CRIT if age is None or age > 50 * 3600 else WARN if age > 26 * 3600 else OK
            if st != OK:
                sec.flag(st, f"CNPG {key}: last completed backup {human_age(age) + ' ago' if age else 'never'}")
            rows.append([cell("CNPG (Barman)"), cell(key), cell(done[-1]["status"]["phase"] if done else "–", st), cell(f"{human_age(age)} ago" if age is not None else "never", st), cell("–")])
    except urllib.error.HTTPError:
        pass
    sec.summary = f"{sum(1 for r in rows if r[2]['status'] == OK)}/{len(rows)} backup sets current"
    sec.facts = [("Current", f"{sum(1 for r in rows if r[2]['status'] == OK)}/{len(rows)}")]
    sec.tables.append(("Latest backups", ["Tool", "Set", "Last result", "Last success", "Size"], rows))


def collect_databases(kube, sec):
    rows = []
    for c in kube.items("postgresql.cnpg.io/v1", "clusters"):
        name = f"{c['metadata']['namespace']}/{c['metadata']['name']}"
        st_obj = c.get("status", {})
        want = c["spec"].get("instances", 1)
        ready = st_obj.get("readyInstances", 0)
        phase = st_obj.get("phase", "")
        st = OK if ready == want and "healthy" in phase.lower() else CRIT if ready == 0 else WARN
        if st != OK:
            sec.flag(st, f"{name}: {ready}/{want} ready ({phase})")
        rows.append([cell(name), cell(f"{ready}/{want}", st), cell(st_obj.get("currentPrimary", "–")), cell(phase, st)])
    sec.summary = f"{sum(1 for r in rows if r[1]['status'] == OK)}/{len(rows)} clusters healthy"
    sec.facts = [("Healthy clusters", f"{sum(1 for r in rows if r[1]['status'] == OK)}/{len(rows)}")]
    sec.tables.append(("CloudNativePG clusters", ["Cluster", "Ready", "Primary", "Phase"], rows))


def collect_certs(kube, sec):
    rows = []
    for c in kube.items("cert-manager.io/v1", "certificates"):
        name = f"{c['metadata']['namespace']}/{c['metadata']['name']}"
        st_obj = c.get("status", {})
        exp = parse_time(st_obj.get("notAfter"))
        renew = parse_time(st_obj.get("renewalTime"))
        ready = condition(c).get("status") == "True"
        days = (exp - NOW).total_seconds() / 86400 if exp else None
        st = OK
        if not ready:
            st = CRIT
            sec.flag(CRIT, f"Certificate {name} is not Ready")
        elif days is not None and days < 1:
            st = CRIT
            sec.flag(CRIT, f"Certificate {name} expires in {days * 24:.0f}h")
        elif renew and renew < NOW - dt.timedelta(hours=12):
            st = WARN
            sec.flag(WARN, f"Certificate {name} is past its renewal time")
        rows.append([cell(name), cell("Ready" if ready else "Not ready", st), cell(local(exp) if exp else "–"), cell(f"{days:.1f} days" if days is not None else "–", st), cell(local(renew) if renew else "–")])
    sec.summary = f"{sum(1 for r in rows if r[1]['status'] == OK)}/{len(rows)} certificates OK"
    sec.facts = [("Certificates OK", f"{sum(1 for r in rows if r[1]['status'] == OK)}/{len(rows)}")]
    sec.tables.append(("cert-manager certificates", ["Certificate", "State", "Expires", "Remaining", "Renews"], rows))


def collect_alerts(sec):
    url = f"{ALERTMANAGER}/api/v2/alerts?active=true&silenced=false&inhibited=false"
    with urllib.request.urlopen(url, timeout=30) as resp:
        alerts = json.load(resp)
    rows = []
    for a in sorted(alerts, key=lambda a: a.get("startsAt", "")):
        lbl = a.get("labels", {})
        name = lbl.get("alertname", "?")
        if name in ("Watchdog", "InfoInhibitor"):
            continue
        sev = lbl.get("severity", "none")
        st = CRIT if sev == "critical" else WARN
        summary = a.get("annotations", {}).get("summary") or a.get("annotations", {}).get("description", "")
        target = lbl.get("namespace", "") + ("/" + (lbl.get("pod") or lbl.get("instance") or "") if (lbl.get("pod") or lbl.get("instance")) else "")
        sec.flag(st, f"{name} ({sev}){': ' + target if target else ''}")
        started = parse_time(a.get("startsAt"))
        rows.append([cell(name, st), cell(sev), cell(target or "–"), cell(summary[:200]), cell(local(started) if started else "–")])
    sec.summary = "No alerts firing" if not rows else f"{len(rows)} alert(s) firing"
    sec.facts = [("Firing", len(rows))]
    if rows:
        sec.tables.append(("Firing alerts", ["Alert", "Severity", "Target", "Summary", "Since"], rows))


def ssh_report(host, user):
    keyfile = os.path.join(tempfile.mkdtemp(), "id")
    shutil.copyfile(SSH_KEY, keyfile)
    os.chmod(keyfile, 0o600)
    cmd = [
        "ssh", "-F", "/dev/null", "-i", keyfile,
        "-o", f"UserKnownHostsFile={CONFIG_DIR}/known_hosts",
        "-o", "StrictHostKeyChecking=yes", "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=15", "-o", "LogLevel=ERROR",
        f"{user}@{host}",
    ]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    finally:
        os.remove(keyfile)
    if out.returncode != 0:
        raise RuntimeError(f"ssh {user}@{host} failed ({out.returncode}): {out.stderr.strip()[:200]}")
    data = {"ctr": []}
    for line in out.stdout.splitlines():
        parts = line.split("\t")
        if parts[0] == "ctr":
            data["ctr"].append(parts[1:])
        elif len(parts) >= 2:
            data[parts[0]] = parts[1]
    return data


def host_common(sec, d, temp_key=None):
    up = int(d.get("uptime_s", 0))
    load = d.get("load", "–")
    total, avail = float(d.get("mem.MemTotal", 0)), float(d.get("mem.MemAvailable", 0))
    mem_pct = 100 * (1 - avail / total) if total else None
    facts = [("Uptime", human_age(up)), ("Load (1/5/15m)", load)]
    if mem_pct is not None:
        facts.append(("Memory used", f"{mem_pct:.0f}% of {human_bytes(total)}"))
        if mem_pct >= 90:
            sec.flag(WARN, f"Memory {mem_pct:.0f}% used")
    ctrs = d.get("ctr", [])
    bad = []
    rows = []
    for c in ctrs:
        name, state, status = (c + ["", ""])[:3]
        st = OK
        if state != "running":
            st = WARN
        if "unhealthy" in status:
            st = CRIT
        if st != OK:
            bad.append(name)
            sec.flag(st, f"Container {name}: {status or state}")
        rows.append([cell(name), cell(state, st if st != OK else None), cell(status, st if st != OK else None)])
    running = sum(1 for c in ctrs if (c + [""])[1] == "running")
    facts.append(("Containers running", f"{running}/{len(ctrs)}"))
    return facts, rows


def collect_unraid(sec):
    d = ssh_report("192.168.1.9", "root")
    facts, ctr_rows = host_common(sec, d)
    if d.get("var.mdState") != "STARTED":
        sec.flag(CRIT, f"Array state is {d.get('var.mdState')}")
    for k, label in (("var.mdNumDisabled", "disabled"), ("var.mdNumInvalid", "invalid"), ("var.mdNumMissing", "missing")):
        if d.get(k, "0") not in ("", "0"):
            sec.flag(CRIT, f"{d[k]} {label} disk(s)")
    if d.get("var.mdResyncPos", "0") not in ("", "0"):
        size = float(d.get("var.mdResyncSize") or 1)
        sec.flag(WARN, f"Parity check/rebuild running ({100 * float(d['var.mdResyncPos']) / size:.0f}%)")
    synced = d.get("var.sbSynced")
    parity_cell = "–"
    if synced and synced != "0":
        last = dt.datetime.fromtimestamp(int(synced), dt.timezone.utc)
        days = (NOW - last).days
        errs = d.get("var.sbSyncErrs", "0")
        parity_cell = f"{last.astimezone(TZ):%b %-d} ({days}d ago, {errs} errors)"
        if d.get("var.sbSyncExit", "0") not in ("0", ""):
            sec.flag(WARN, f"Last parity check did not complete (exit {d['var.sbSyncExit']})")
        if errs not in ("0", ""):
            sec.flag(WARN, f"Last parity check found {errs} errors")
        if days > 35:
            sec.flag(WARN, f"Last parity check was {days} days ago")
    facts.insert(0, ("Array", d.get("var.mdState", "?").title()))
    facts.insert(1, ("Last parity check", parity_cell))
    for key, label in (("df./mnt/user", "Array used"), ("df./mnt/cache", "Cache used")):
        if key in d:
            size, used = (float(x) for x in d[key].split())
            pct = 100 * used / size
            facts.append((label, f"{human_bytes(used)} of {human_bytes(size)} ({pct:.0f}%)"))
            if pct >= 85:
                sec.flag(WARN if pct < 95 else CRIT, f"{label}: {pct:.0f}%")
    disks = {}
    for k, v in d.items():
        if k.startswith("disk."):
            _, name, field = k.split(".", 2)
            disks.setdefault(name, {})[field] = v
    rows = []
    order = {"Parity": 0, "Data": 1, "Cache": 2, "Flash": 3}
    for name, f in sorted(disks.items(), key=lambda kv: (order.get(kv[1].get("type", ""), 9), kv[0])):
        if not f.get("device") and f.get("status", "").endswith("_NP"):
            continue  # empty slot
        status = f.get("status", "")
        st = OK if status == "DISK_OK" else CRIT
        if st != OK:
            sec.flag(CRIT, f"Disk {name}: {status}")
        errs = f.get("numErrors", "0")
        if errs not in ("0", ""):
            st = worst(st, WARN)
            sec.flag(WARN, f"Disk {name}: {errs} read/write errors")
        t = f.get("temp", "*")
        t_cell = cell("spun down")
        if t not in ("*", ""):
            tc = float(t)
            ts = CRIT if tc >= 55 else WARN if tc >= 50 else None
            if ts:
                sec.flag(ts, f"Disk {name} at {c_to_f(tc)}°F")
            t_cell = cell(f"{c_to_f(tc)}°F", ts)
        use = "–"
        if f.get("fsSize") and f.get("fsUsed"):
            sz, us = float(f["fsSize"]) * 1024, float(f["fsUsed"]) * 1024
            use = f"{human_bytes(us)} / {human_bytes(sz)} ({100 * us / sz:.0f}%)" if sz else "–"
        rows.append([cell(name), cell(f.get("type", "")), cell(status.replace("DISK_", ""), st), t_cell, cell(errs), cell(use)])
    sec.summary = f"Array {d.get('var.mdState', '?').lower()}, {len(rows)} disks, {len(ctr_rows)} containers"
    sec.facts = facts
    sec.tables.append(("Disks", ["Disk", "Type", "Status", "Temp", "Errors", "Used"], rows))
    sec.tables.append(("Containers", ["Container", "State", "Status"], ctr_rows))


def collect_pi5(sec):
    d = ssh_report("192.168.0.209", "jeff")
    facts, ctr_rows = host_common(sec, d)
    if "cpu_temp_c" in d:
        t = float(d["cpu_temp_c"])
        facts.append(("CPU temp", f"{c_to_f(t)}°F"))
        if t >= 75:
            sec.flag(WARN, f"CPU at {c_to_f(t)}°F")
    if "df./" in d:
        size, used = (float(x) for x in d["df./"].split())
        pct = 100 * used / size
        facts.append(("Disk used", f"{human_bytes(used)} of {human_bytes(size)} ({pct:.0f}%)"))
        if pct >= 85:
            sec.flag(WARN, f"Disk {pct:.0f}% full")
    sec.summary = f"{d.get('host', 'pi5')}: up {human_age(int(d.get('uptime_s', 0)))}, {len(ctr_rows)} containers"
    sec.facts = facts
    sec.tables.append(("Containers", ["Container", "State", "Status"], ctr_rows))


class PinnedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPS to a given IP while sending SNI and Host for the real hostname."""

    def __init__(self, host, ip, **kw):
        super().__init__(host, **kw)
        self._ip = ip

    def connect(self):
        sock = socket.create_connection((self._ip, self.port or 443), self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


def probe(url, ip=None, verify=True):
    u = urllib.parse.urlsplit(url)
    ctx = ssl.create_default_context() if verify else ssl._create_unverified_context()
    start = time.monotonic()
    if u.scheme == "https":
        conn = PinnedHTTPSConnection(u.hostname, ip, port=u.port, timeout=15, context=ctx) if ip else http.client.HTTPSConnection(u.hostname, port=u.port, timeout=15, context=ctx)
    else:
        conn = http.client.HTTPConnection(ip or u.hostname, port=u.port, timeout=15)
    try:
        conn.request("GET", u.path or "/", headers={"Host": u.netloc, "User-Agent": "homelab-status/1"})
        resp = conn.getresponse()
        resp.read(1024)
        ms = (time.monotonic() - start) * 1000
        days = None
        if u.scheme == "https" and verify:
            cert = conn.sock.getpeercert()
            if cert and cert.get("notAfter"):
                exp = dt.datetime.fromtimestamp(ssl.cert_time_to_seconds(cert["notAfter"]), dt.timezone.utc)
                days = (exp - NOW).total_seconds() / 86400
        return resp.status, ms, days, None
    except Exception as exc:
        return None, (time.monotonic() - start) * 1000, None, f"{type(exc).__name__}: {exc}"[:160]
    finally:
        conn.close()


def collect_services(kube, sec):
    targets = []
    gateways = {}
    try:
        for g in kube.items("gateway.networking.k8s.io/v1", "gateways", "network"):
            addrs = g.get("status", {}).get("addresses") or []
            if addrs:
                gateways[g["metadata"]["name"]] = addrs[0]["value"]
    except Exception:
        pass
    seen = set()
    for r in kube.items("gateway.networking.k8s.io/v1", "httproutes"):
        parent = (r["spec"].get("parentRefs") or [{}])[0].get("name", "")
        for h in r["spec"].get("hostnames") or []:
            if h in seen:
                continue
            seen.add(h)
            internal = parent == "envoy-internal"
            targets.append({
                "name": h,
                "url": f"https://{h}/",
                "group": "Cluster (LAN only)" if internal else "Cluster",
                "ip": gateways.get(parent) if internal else None,
            })
    try:
        with open(f"{CONFIG_DIR}/probes.json") as f:
            targets += json.load(f)
    except FileNotFoundError:
        pass
    rows = []
    up = 0
    for t in sorted(targets, key=lambda x: (x.get("group", ""), x["name"])):
        code, ms, days, err = probe(t["url"], t.get("ip"), t.get("verify", True))
        ok = code is not None and code < 500
        st = OK if ok else CRIT if t.get("critical", True) else WARN
        if ok:
            up += 1
            if ms > 3000:
                st = WARN
                sec.flag(WARN, f"{t['name']} slow ({ms:.0f} ms)")
        else:
            sec.flag(st, f"{t['name']} down: {err or code}")
        cert = "–"
        if days is not None:
            cert = f"{days:.0f}d"
            if days < 2:
                st = worst(st, WARN)
                sec.flag(WARN, f"{t['name']} TLS certificate expires in {days:.1f} days")
        rows.append([cell(t["name"]), cell(t.get("group", "")), cell(code if code else "down", st), cell(f"{ms:.0f} ms"), cell(cert), cell(err or "")])
    sec.summary = f"{up}/{len(rows)} endpoints responding"
    sec.facts = [("Responding", f"{up}/{len(rows)}")]
    sec.tables.append(("Endpoints", ["Endpoint", "Group", "HTTP", "Time", "TLS left", "Error"], rows))


# --------------------------------------------------------------- render

CSS = """
:root{--bg:#f5f6f8;--card:#fff;--fg:#1d2330;--muted:#5d6676;--line:#e3e6eb;--ok:#1a7f4b;--ok-bg:#e3f5ea;--warn:#a15c00;--warn-bg:#fff1d6;--crit:#b42318;--crit-bg:#fde4e1;--unknown:#5d6676;--unknown-bg:#eceef2}
@media (prefers-color-scheme:dark){:root{--bg:#0f1218;--card:#171b23;--fg:#e6e9ef;--muted:#98a1b0;--line:#272d38;--ok:#4cc38a;--ok-bg:#12291d;--warn:#f0b04a;--warn-bg:#2e2412;--crit:#f97066;--crit-bg:#341614;--unknown:#98a1b0;--unknown-bg:#222833}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
main{max-width:1180px;margin:0 auto;padding:28px 20px 60px}
header.top{display:flex;flex-wrap:wrap;align-items:center;gap:16px;justify-content:space-between;margin-bottom:22px}
h1{font-size:24px;margin:0;letter-spacing:-.01em}
.sub{color:var(--muted);font-size:13.5px;margin-top:2px}
.pill{display:inline-flex;align-items:center;gap:8px;padding:6px 14px;border-radius:999px;font-weight:600;font-size:14px}
.pill::before{content:"";width:9px;height:9px;border-radius:50%;background:currentColor}
.s-ok{color:var(--ok);background:var(--ok-bg)}.s-warn{color:var(--warn);background:var(--warn-bg)}.s-crit{color:var(--crit);background:var(--crit-bg)}.s-unknown{color:var(--unknown);background:var(--unknown-bg)}
.t-ok{color:var(--ok)}.t-warn{color:var(--warn);font-weight:600}.t-crit{color:var(--crit);font-weight:600}.t-unknown{color:var(--unknown)}
.overview{display:grid;grid-template-columns:repeat(auto-fill,minmax(170px,1fr));gap:12px;margin-bottom:18px}
.ov{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px 14px;text-decoration:none;color:inherit;border-left:4px solid var(--line)}
.ov:hover{border-color:var(--muted)}
.ov.b-ok{border-left-color:var(--ok)}.ov.b-warn{border-left-color:var(--warn)}.ov.b-crit{border-left-color:var(--crit)}.ov.b-unknown{border-left-color:var(--unknown)}
.ov .name{font-size:12.5px;color:var(--muted);text-transform:uppercase;letter-spacing:.04em}
.ov .val{font-size:15px;font-weight:600;margin-top:2px}
.findings{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 18px;margin-bottom:18px}
.findings h2{font-size:15px;margin:0 0 8px}
.findings ul{margin:0;padding-left:0;list-style:none}
.findings li{padding:3px 0;display:flex;gap:10px;align-items:baseline}
.dot{width:8px;height:8px;border-radius:50%;flex:none;display:inline-block;transform:translateY(-1px)}
.d-ok{background:var(--ok)}.d-warn{background:var(--warn)}.d-crit{background:var(--crit)}.d-unknown{background:var(--unknown)}
.trend{display:flex;gap:3px;align-items:center;margin-top:6px}
.trend span{width:14px;height:14px;border-radius:3px;display:inline-block}
section.card{background:var(--card);border:1px solid var(--line);border-radius:12px;margin-bottom:16px;overflow:hidden}
section.card>header{display:flex;align-items:center;gap:12px;padding:14px 18px;border-bottom:1px solid var(--line)}
section.card>header h2{font-size:17px;margin:0;flex:1}
section.card>header .summary{color:var(--muted);font-size:13.5px}
.body{padding:14px 18px}
.facts{display:flex;flex-wrap:wrap;gap:10px 28px;margin-bottom:10px}
.fact .k{font-size:12px;color:var(--muted);text-transform:uppercase;letter-spacing:.04em}
.fact .v{font-weight:600}
.notes{margin:4px 0 12px;padding:0;list-style:none}
.notes li{display:flex;gap:10px;align-items:baseline;padding:2px 0}
.tablewrap{overflow-x:auto;margin-top:6px}
table{border-collapse:collapse;width:100%;font-size:13.5px}
caption{text-align:left;font-weight:600;font-size:13.5px;color:var(--muted);padding:6px 0}
th,td{text-align:left;padding:6px 10px;border-bottom:1px solid var(--line);white-space:nowrap}
td.wrap{white-space:normal;min-width:240px}
th{font-size:12px;color:var(--muted);text-transform:uppercase;letter-spacing:.04em;font-weight:600}
tr:last-child td{border-bottom:none}
details>summary{cursor:pointer;color:var(--muted);font-size:13.5px;margin:6px 0}
footer{color:var(--muted);font-size:12.5px;margin-top:26px;text-align:center}
"""


def esc(x):
    return html.escape(str(x))


def render_table(caption, headers, rows):
    out = [f'<div class="tablewrap"><table><caption>{esc(caption)}</caption><thead><tr>']
    out += [f"<th>{esc(h)}</th>" for h in headers]
    out.append("</tr></thead><tbody>")
    for r in rows:
        out.append("<tr>")
        for c in r:
            if not isinstance(c, dict):
                c = cell(c)
            cls = f' class="t-{c["status"]}"' if c.get("status") else ""
            wrap = " wrap" if len(c["text"]) > 60 else ""
            if wrap:
                cls = f' class="{("t-" + c["status"]) if c.get("status") else ""}{wrap}"'
            out.append(f"<td{cls}>{esc(c['text'])}</td>")
        out.append("</tr>")
    out.append("</tbody></table></div>")
    return "".join(out)


def render(sections, overall, history, duration):
    gen = local(NOW)
    findings = [(s, t, sec.title) for sec in sections for s, t in sec.notes if s != OK]
    findings.sort(key=lambda x: -RANK[x[0]])
    parts = [
        "<!doctype html><html lang=en><head><meta charset=utf-8>",
        '<meta name=viewport content="width=device-width,initial-scale=1">',
        f"<title>{esc(LABEL[overall])} · Homelab status</title>",
        f"<style>{CSS}</style></head><body><main>",
        '<header class="top"><div><h1>Homelab status</h1>',
        f'<div class="sub">Generated {esc(gen)} in {duration:.0f}s · refreshes at 6:00 AM and 6:00 PM</div></div>',
        f'<span class="pill s-{overall}">{esc(LABEL[overall])}</span></header>',
        '<div class="overview">',
    ]
    for sec in sections:
        parts.append(f'<a class="ov b-{sec.status}" href="#{sec.key}"><div class="name">{esc(sec.title)}</div><div class="val t-{sec.status}">{esc(LABEL[sec.status])}</div><div class="sub">{esc(sec.summary)}</div></a>')
    parts.append("</div>")
    parts.append('<div class="findings"><h2>Findings</h2>')
    if findings:
        parts.append("<ul>")
        for s, t, title in findings[:40]:
            parts.append(f'<li><span class="dot d-{s}"></span><span><b>{esc(title)}:</b> {esc(t)}</span></li>')
        parts.append("</ul>")
    else:
        parts.append('<div class="t-ok">Nothing needs attention.</div>')
    if history:
        parts.append('<div class="sub" style="margin-top:12px">Last 30 days</div><div class="trend">')
        for h in history[-HISTORY_DAYS:]:
            parts.append(f'<span class="d-{h["overall"]}" title="{esc(h["date"])}: {esc(LABEL[h["overall"]])}, {h.get("warn", 0)} warnings, {h.get("crit", 0)} critical"></span>')
        parts.append("</div>")
    parts.append("</div>")
    for sec in sections:
        parts.append(f'<section class="card" id="{sec.key}"><header><span class="pill s-{sec.status}">{esc(LABEL[sec.status])}</span><h2>{esc(sec.title)}</h2><span class="summary">{esc(sec.summary)}</span></header><div class="body">')
        if sec.facts:
            parts.append('<div class="facts">' + "".join(f'<div class="fact"><div class="k">{esc(k)}</div><div class="v">{esc(v)}</div></div>' for k, v in sec.facts) + "</div>")
        notes = [(s, t) for s, t in sec.notes if s != OK]
        if notes:
            parts.append('<ul class="notes">' + "".join(f'<li><span class="dot d-{s}"></span>{esc(t)}</li>' for s, t in notes) + "</ul>")
        for i, (cap, headers, rows) in enumerate(sec.tables):
            if not rows:
                continue
            table = render_table(cap, headers, rows)
            # Long, all-healthy tables start collapsed
            if len(rows) > 8 and not any(isinstance(c, dict) and c.get("status") in (WARN, CRIT) for r in rows for c in r):
                parts.append(f"<details><summary>{esc(cap)} ({len(rows)})</summary>{table}</details>")
            else:
                parts.append(table)
        parts.append("</div></section>")
    parts.append('<footer>homelab-status · home-ops <code>kubernetes/apps/observability/homelab-status</code> · run now: <code>kubectl -n observability create job --from=cronjob/homelab-status homelab-status-manual</code></footer>')
    parts.append("</main></body></html>")
    return "".join(parts)


def main():
    start = time.monotonic()
    kube = Kube()
    plan = [
        ("alerts", "Alerts", lambda s: collect_alerts(s)),
        ("nodes", "Talos cluster", lambda s: collect_nodes(kube, s)),
        ("flux", "Flux GitOps", lambda s: collect_flux(kube, s)),
        ("workloads", "Workloads", lambda s: collect_workloads(kube, s)),
        ("storage", "Longhorn storage", lambda s: collect_storage(kube, s)),
        ("databases", "Databases", lambda s: collect_databases(kube, s)),
        ("backups", "Backups", lambda s: collect_backups(kube, s)),
        ("certs", "Certificates", lambda s: collect_certs(kube, s)),
        ("services", "Services", lambda s: collect_services(kube, s)),
        ("unraid", "unraid", lambda s: collect_unraid(s)),
        ("pi5", "pi5", lambda s: collect_pi5(s)),
    ]
    sections = []
    for key, title, fn in plan:
        sec = Section(key, title)
        try:
            fn(sec)
        except Exception as exc:
            traceback.print_exc()
            sec.status = UNKNOWN
            sec.summary = "Check failed"
            sec.notes.append((UNKNOWN, f"Could not collect: {type(exc).__name__}: {str(exc)[:200]}"))
        print(f"{title}: {sec.status} - {sec.summary}", flush=True)
        sections.append(sec)
    overall = worst(*(s.status for s in sections))

    path = f"/api/v1/namespaces/{NAMESPACE}/configmaps/{PAGE_CONFIGMAP}"
    existing = None
    try:
        existing = kube.get(path)
    except urllib.error.HTTPError as exc:
        if exc.code != 404:
            raise
    history = []
    if existing:
        try:
            history = json.loads(existing.get("data", {}).get("history.json", "[]"))
        except ValueError:
            history = []
    today = NOW.astimezone(TZ).date().isoformat()
    counts = {"warn": sum(1 for s in sections for st, _ in s.notes if st == WARN), "crit": sum(1 for s in sections for st, _ in s.notes if st == CRIT)}
    # One entry per day holding that day's worst run, so a clean evening run
    # doesn't hide a bad morning.
    entry = {"date": today, "overall": overall, **counts}
    for h in history:
        if h.get("date") == today:
            entry = {
                "date": today,
                "overall": worst(h.get("overall", OK), overall),
                "warn": max(h.get("warn", 0), counts["warn"]),
                "crit": max(h.get("crit", 0), counts["crit"]),
            }
    history = [h for h in history if h.get("date") != today] + [entry]
    history = history[-HISTORY_DAYS:]

    duration = time.monotonic() - start
    page = render(sections, overall, history, duration)
    status = {"generated": NOW.isoformat(), "overall": overall, "sections": [s.as_dict() for s in sections]}
    body = {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {"name": PAGE_CONFIGMAP, "namespace": NAMESPACE, "labels": {"app.kubernetes.io/name": "homelab-status"}},
        "data": {"index.html": page, "status.json": json.dumps(status, indent=1), "history.json": json.dumps(history)},
    }
    if existing:
        body["metadata"]["resourceVersion"] = existing["metadata"]["resourceVersion"]
        kube.request("PUT", path, body)
    else:
        kube.request("POST", f"/api/v1/namespaces/{NAMESPACE}/configmaps", body)
    print(f"Overall: {overall}; wrote {len(page)} bytes to {NAMESPACE}/{PAGE_CONFIGMAP}", flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        sys.exit(1)
