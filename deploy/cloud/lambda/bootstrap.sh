#!/usr/bin/env bash
# One-shot setup of the observability stack on a single Lambda Cloud GPU node.
#
#   cd ~/otel-observability && ./deploy/cloud/lambda/bootstrap.sh
#
# Installs k3s (+ NVIDIA runtime), Helm, builds the full collector and demo images
# on the node, deploys the Helm chart with values-lambda.yaml, and records a
# hardware inventory (GPUs, NVLink, InfiniBand, NVMe) in results/preflight.txt.
# Safe to re-run.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
RESULTS="$ROOT/results"
HELM_VERSION="${HELM_VERSION:-v4.3.2}"
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml

log() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
die() { printf '\033[1;31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }
mkdir -p "$RESULTS"

# ----------------------------------------------------------------------------- preflight
log "Preflight: hardware inventory -> results/preflight.txt"
command -v nvidia-smi >/dev/null || die "nvidia-smi not found: is this a GPU instance?"
{
  echo "# $(date -u +%FT%TZ)  $(hostname)"
  echo "## GPUs"
  nvidia-smi --query-gpu=index,name,driver_version,memory.total,pcie.link.gen.max,pcie.link.width.max --format=csv
  echo; echo "## Topology (NV# = NVLink, PIX/PXB/SYS = PCIe paths)"
  nvidia-smi topo -m || true
  echo; echo "## NVLink"
  if nvidia-smi nvlink -s 2>/dev/null | grep -q 'GB/s'; then nvidia-smi nvlink -s | head -40; else echo "not detected"; fi
  echo; echo "## InfiniBand / RoCE adapters"
  if [ -d /sys/class/infiniband ] && [ -n "$(ls -A /sys/class/infiniband 2>/dev/null)" ]; then
    for d in /sys/class/infiniband/*; do
      for p in "$d"/ports/*; do
        echo "$(basename "$d") port $(basename "$p"): $(cat "$p/state" 2>/dev/null) $(cat "$p/rate" 2>/dev/null) link_layer=$(cat "$p/link_layer" 2>/dev/null)"
      done
    done
  else echo "none"; fi
  echo; echo "## Block devices"
  lsblk -d -o NAME,SIZE,MODEL,TRAN,ROTA
  echo; echo "## Filesystems"
  df -h -x tmpfs -x devtmpfs -x overlay -x squashfs
} | tee "$RESULTS/preflight.txt"

GPUS=$(nvidia-smi -L | wc -l)
log "Found $GPUS GPU(s)"

# ----------------------------------------------------------------------------- docker + NVIDIA toolkit
DOCKER="docker"
docker info >/dev/null 2>&1 || DOCKER="sudo docker"
$DOCKER info >/dev/null 2>&1 || die "Docker isn't available"

if ! command -v nvidia-container-runtime >/dev/null; then
  log "Installing NVIDIA container toolkit"
  curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey \
    | sudo gpg --dearmor --yes -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
  curl -fsSL https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
    | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
    | sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list >/dev/null
  sudo apt-get update -qq && sudo apt-get install -y -qq nvidia-container-toolkit
fi

# ----------------------------------------------------------------------------- k3s
if ! command -v k3s >/dev/null; then
  log "Installing k3s (traefik/servicelb disabled; detects the NVIDIA runtime at start)"
  curl -sfL https://get.k3s.io | INSTALL_K3S_EXEC="server --disable traefik --disable servicelb --write-kubeconfig-mode 644" sh -
fi
log "Waiting for the k3s node"
for _ in $(seq 1 60); do kubectl get nodes 2>/dev/null | grep -q ' Ready' && break; sleep 5; done
kubectl get nodes

if ! sudo grep -rqs nvidia /var/lib/rancher/k3s/agent/etc/containerd/; then
  log "k3s hasn't registered the nvidia runtime yet; restarting k3s"
  sudo systemctl restart k3s
  sleep 15
  sudo grep -rqs nvidia /var/lib/rancher/k3s/agent/etc/containerd/ || die "nvidia runtime not found in k3s containerd config"
fi
kubectl get runtimeclass nvidia >/dev/null 2>&1 || kubectl apply -f - <<'EOF'
apiVersion: node.k8s.io/v1
kind: RuntimeClass
metadata:
  name: nvidia
handler: nvidia
EOF

# ----------------------------------------------------------------------------- helm
if ! command -v helm >/dev/null; then
  log "Installing Helm $HELM_VERSION"
  curl -fsSL "https://get.helm.sh/helm-${HELM_VERSION}-linux-amd64.tar.gz" | tar -xz -C /tmp
  sudo install -m 0755 /tmp/linux-amd64/helm /usr/local/bin/helm
fi

# ----------------------------------------------------------------------------- images
import_image() { $DOCKER save "$1" | sudo k3s ctr -n k8s.io images import - >/dev/null && echo "imported $1"; }

log "Building full collector (OCB + prometheusreceiver) and demo services"
$DOCKER build --build-arg MANIFEST=builder-config.cloud.yaml -t obsdemo/otelcol-obs:0.1.0-full "$ROOT/collector"
$DOCKER build -t obsdemo/services:0.1.0 "$ROOT/services"
import_image obsdemo/otelcol-obs:0.1.0-full
import_image obsdemo/services:0.1.0

# ----------------------------------------------------------------------------- deploy
log "Deploying obs-stack (values-lambda.yaml)"
CHART="$ROOT/deploy/helm/obs-stack"
helm repo add grafana https://grafana.github.io/helm-charts >/dev/null 2>&1 || true
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts >/dev/null 2>&1 || true
helm repo update >/dev/null
helm dependency build "$CHART" >/dev/null
helm upgrade --install obs "$CHART" -n observability --create-namespace \
  -f "$CHART/values-lambda.yaml" --wait --timeout 15m
# Agent first came up before the gateway on a fresh install: restart it so its
# exporters connect cleanly (see docs/DECISIONS.md).
kubectl -n observability rollout restart ds/obs-otel-agent >/dev/null
kubectl -n observability rollout status ds/obs-otel-agent --timeout=120s

kubectl get pods -A
log "Done. Next:"
cat <<EOF
  1. On the node:    ./deploy/cloud/lambda/ui.sh &       (binds Grafana/Prometheus/ClickHouse to localhost)
  2. On your laptop: ssh -N -L 3000:localhost:3000 -L 9090:localhost:9090 -L 8123:localhost:8123 ubuntu@<instance-ip>
                     then open http://localhost:3000 (admin / admin)
  3. On the node:    ./deploy/cloud/lambda/verify.sh     (checks which data is arriving)
  4. On the node:    ./deploy/cloud/lambda/loadtests.sh  (GPU burn, NCCL all-reduce, fio)
  5. On the node:    ./deploy/cloud/lambda/export.sh     (BEFORE terminating the instance)
EOF
