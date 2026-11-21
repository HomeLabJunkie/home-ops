#!/usr/bin/env python3
"""Check every app that includes components/kopiur/backup keeps its contract.

Reads `flate build all` output as JSON lines on stdin, e.g.

  flate build all -p ./kubernetes/apps | yq -o=json -I=0 '.' \
    | python3 .github/scripts/check-kopiur-callers.py

For each Flux Kustomization with the component:
  error    APP is set (postBuild.substitute)
  error    it depends, directly or through other Kustomizations, on
           kopiur-system/kopiur-repository and longhorn-system/longhorn
  error    a HelmRelease named ${APP} is in it, and one of its workloads
           mounts the PVC ${APP} (the component creates it; app-template's
           existingClaim: "{{ .Release.Name }}" names it)
  warning  that workload runs as KOPIUR_PUID (default 1000), the user the
           backup mover reads files as; otherwise files the app writes
           without read access for that user are left out of backups
Errors fail the job; both print as GitHub annotations.
"""
import json
import sys

COMPONENT = "components/kopiur/backup"
WORKLOADS = {"Deployment", "StatefulSet", "DaemonSet"}

docs = [json.loads(line) for line in sys.stdin if line.strip() not in ("", "null")]
docs = [d for d in docs if isinstance(d, dict) and d.get("kind")]


def ref(d):
    return (d["metadata"].get("namespace", ""), d["metadata"]["name"])


kustomizations = {
    ref(d): d
    for d in docs
    if d["kind"] == "Kustomization" and d.get("apiVersion", "").startswith("kustomize.toolkit.fluxcd.io/")
}


def deps(key, seen=None):
    seen = set() if seen is None else seen
    ks = kustomizations.get(key)
    for dep in (ks or {}).get("spec", {}).get("dependsOn", []) or []:
        d = (dep.get("namespace", key[0]), dep["name"])
        if d not in seen:
            seen.add(d)
            deps(d, seen)
    return seen


def run_as_user(pod):
    users = {c.get("securityContext", {}).get("runAsUser") for c in pod.get("containers", [])}
    pod_user = pod.get("securityContext", {}).get("runAsUser")
    users = {u if u is not None else pod_user for u in users}
    return users


errors = warnings = 0
callers = [k for k, d in kustomizations.items() if any(str(c).rstrip("/").endswith(COMPONENT) for c in d["spec"].get("components", []) or [])]
for key in sorted(callers):
    ks = kustomizations[key]
    name = "/".join(key)
    subs = (ks["spec"].get("postBuild") or {}).get("substitute") or {}
    app = subs.get("APP")
    target = ks["spec"].get("targetNamespace", key[0])

    def err(msg):
        global errors
        errors += 1
        print(f"::error title=Kopiur backup contract::Kustomization {name}: {msg}")

    def warn(msg):
        global warnings
        warnings += 1
        print(f"::warning title=Kopiur backup contract::Kustomization {name}: {msg}")

    if not app:
        err("includes the kopiur/backup component but sets no postBuild.substitute.APP")
        continue
    reach = deps(key)
    for need in [("kopiur-system", "kopiur-repository"), ("longhorn-system", "longhorn")]:
        if need not in reach:
            err(f"must depend on {'/'.join(need)} (directly or through another Kustomization)")

    owned = [d for d in docs if d["metadata"].get("labels", {}).get("kustomize.toolkit.fluxcd.io/name") == key[1]
             and d["metadata"].get("labels", {}).get("kustomize.toolkit.fluxcd.io/namespace") == key[0]]
    if not any(d["kind"] == "HelmRelease" and d["metadata"]["name"] == app for d in owned):
        err(f"has no HelmRelease named {app} (APP)")
        continue
    mounting = []
    for d in docs:
        labels = d["metadata"].get("labels", {})
        if d["kind"] not in WORKLOADS or labels.get("helm.toolkit.fluxcd.io/name") != app \
                or labels.get("helm.toolkit.fluxcd.io/namespace") != target:
            continue
        pod = d["spec"]["template"]["spec"]
        claims = {v["persistentVolumeClaim"]["claimName"] for v in pod.get("volumes", []) or [] if v.get("persistentVolumeClaim")}
        if app in claims:
            mounting.append((d, pod))
    if not mounting:
        err(f"no workload of HelmRelease {app} mounts the PVC {app} that the component creates")
        continue
    puid = int(subs.get("KOPIUR_PUID", 1000))
    for d, pod in mounting:
        users = run_as_user(pod)
        if users != {puid}:
            shown = ", ".join("image default (often root)" if u is None else str(u) for u in sorted(users, key=str))
            warn(f"{d['kind']} {d['metadata']['name']} runs as {shown}, but the backup mover reads its files as "
                 f"{puid} (KOPIUR_PUID); files not readable by {puid} are skipped")

print(f"kopiur/backup callers: {len(callers)} checked, {errors} error(s), {warnings} warning(s)")
sys.exit(1 if errors else 0)
