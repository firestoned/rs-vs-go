#!/usr/bin/env bash
# Emit N ConfigMaps as a multi-document YAML stream with a deterministic payload.
# Usage: gen-configmaps.sh <namespace> <count> <payload-bytes>
set -euo pipefail
ns=$1 count=$2 size=$3
payload=$(head -c "$size" /dev/zero | tr '\0' 'x')
for ((i = 0; i < count; i++)); do
  printf -- '---\napiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: churn-%04d\n  namespace: %s\n  labels:\n    cmbench/churn: "true"\ndata:\n  payload: "%s"\n' \
    "$i" "$ns" "$payload"
done
