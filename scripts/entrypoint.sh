#!/usr/bin/env bash
set -e
if [ ! -r "${KUBECONFIG:-}" ]; then
  echo "⚠️  No hay kubeconfig en \$KUBECONFIG ($KUBECONFIG). Móntalo con -v ~/.kube/config:$KUBECONFIG:ro" >&2
fi
exec "$@"
