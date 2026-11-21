#!/usr/bin/env bash
# Checks that bootstrap still works against the files it reads from
# kubernetes/apps by path: renders both helmfiles, checks each release's
# app/ocirepository.yaml (chart) and app/helmrelease.yaml (values) exist, and
# checks every file the apps.yaml hooks `kubectl apply`. Needs helmfile,
# helm and yq; no cluster. Run from the repository root.
set -euo pipefail
cd bootstrap/helmfile

for f in crds.yaml apps.yaml; do
  docs=$(KUBECONFIG=/dev/null helmfile -f "$f" template -q | grep -c '^kind:' || true)
  if [[ "$docs" -eq 0 ]]; then
    echo "::error file=bootstrap/helmfile/$f::helmfile template rendered nothing"
    exit 1
  fi
  echo "helmfile template $f: $docs objects"
done

missing=0
# Each release's chart and values (templates/*.gotmpl). A missing values file
# does not fail helmfile: the release would install with chart defaults.
for f in crds.yaml apps.yaml; do
  while IFS= read -r release; do
    for file in ocirepository.yaml helmrelease.yaml; do
      path="../../kubernetes/apps/$release/app/$file"
      if [[ ! -f "$path" ]]; then
        echo "::error file=bootstrap/helmfile/$f::release $release reads ${path#../../}, which does not exist"
        missing=1
      fi
    done
  done < <(yq '.releases[] | .namespace + "/" + .name' "$f")
done
echo "release chart and values files checked"

while IFS= read -r path; do
  if [[ -f "$path" ]]; then
    echo "hook file ok: ${path#../../}"
  else
    echo "::error file=bootstrap/helmfile/apps.yaml::a hook applies ${path#../../}, which does not exist"
    missing=1
  fi
done < <(yq '.releases[] | .namespace as $ns | .name as $n | .hooks[]?.args[]?
  | select(test("^--filename="))
  | sub("^--filename="; "")
  | sub("\{\{ \.Release\.Namespace \}\}"; $ns)
  | sub("\{\{ \.Release\.Name \}\}"; $n)' apps.yaml)
exit "$missing"
