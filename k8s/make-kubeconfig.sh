#!/usr/bin/env bash
# Genera por stdout un kubeconfig que usa el ServiceAccount de solo lectura de k8s/rbac.yaml.
# Uso: ./k8s/make-kubeconfig.sh > ~/.kube/k8s-inspector.config && chmod 600 ~/.kube/k8s-inspector.config
# Variables: NS (k8s-inspector), SA_SECRET (k8s-inspector-token), CLUSTER_NAME, KUBECTL_CONTEXT (contexto admin a usar)
set -euo pipefail
NS=${NS:-k8s-inspector}
SECRET=${SA_SECRET:-k8s-inspector-token}
K=(kubectl ${KUBECTL_CONTEXT:+--context "$KUBECTL_CONTEXT"})

TOKEN=$("${K[@]}" -n "$NS" get secret "$SECRET" -o jsonpath='{.data.token}' | base64 -d)
[ -n "$TOKEN" ] || { echo "El Secret $NS/$SECRET aún no tiene token (¿aplicaste k8s/rbac.yaml?)" >&2; exit 1; }
CA=$("${K[@]}" -n "$NS" get secret "$SECRET" -o jsonpath='{.data.ca\.crt}')
SERVER=$("${K[@]}" config view --minify -o jsonpath='{.clusters[0].cluster.server}')
CLUSTER=${CLUSTER_NAME:-$("${K[@]}" config view --minify -o jsonpath='{.clusters[0].name}')}

cat <<YAML
apiVersion: v1
kind: Config
clusters:
  - name: ${CLUSTER}
    cluster:
      server: ${SERVER}
      certificate-authority-data: ${CA}
users:
  - name: k8s-inspector
    user:
      token: ${TOKEN}
contexts:
  - name: k8s-inspector@${CLUSTER}
    context:
      cluster: ${CLUSTER}
      user: k8s-inspector
current-context: k8s-inspector@${CLUSTER}
YAML
