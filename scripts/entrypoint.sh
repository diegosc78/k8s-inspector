#!/usr/bin/env bash
set -e
[ "${1:-}" = holmes-gateway ] && exec "$@"   # el gateway no usa Kubernetes
if [ -n "${KUBERNETES_SERVICE_HOST:-}" ] && [ ! -r "${KUBECONFIG:-}" ]; then
  unset KUBECONFIG   # dentro del clúster: kubectl usa el ServiceAccount del pod
elif [ ! -r "${KUBECONFIG:-}" ]; then
  echo "⚠️  No hay kubeconfig en \$KUBECONFIG ($KUBECONFIG). Móntalo con -v ~/.kube/config:$KUBECONFIG:ro" >&2
fi
exec "$@"
