#!/bin/sh
# Validate the rendered manifests with kubeconform (strict): Kubernetes kinds
# against the default schemas, OpenShift kinds (Route, BuildConfig,
# ImageStream) against the OpenShift 4.18 standalone schemas.
set -eu
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
SCHEMAS=$(mktemp -d)
trap 'rm -rf "$SCHEMAS"' EXIT
BASE=https://raw.githubusercontent.com/melmorabity/openshift-json-schemas/main/v4.18-standalone-strict
for pair in route:route buildconfig:build imagestream:image; do
  kind=${pair%%:*}; group=${pair##*:}
  curl -sfL "$BASE/$kind-$group-v1.json" -o "$SCHEMAS/$kind-$group.openshift.io-v1.json"
done
for overlay in kubernetes openshift; do
  echo "== $overlay"
  kubectl kustomize "$ROOT/deployment/$overlay" | kubeconform -strict -summary \
    -schema-location default \
    -schema-location "$SCHEMAS/{{.ResourceKind}}-{{.Group}}-{{.ResourceAPIVersion}}.json" -
done
