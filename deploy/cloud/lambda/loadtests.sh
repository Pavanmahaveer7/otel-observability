#!/usr/bin/env bash
# Drives the node through phases so every layer shows up on the dashboards:
#
#   baseline -> gpu-burn -> nccl-allreduce -> storage-fio -> cooldown
#
# Each phase start/end is sent as an OTLP log (ServiceName='loadtest') so it can
# be lined up with GPU, NVLink, PCIe, disk and InfiniBand metrics in ClickHouse.
# Raw tool output goes to results/.
#
# Durations (seconds) can be overridden, e.g.  BURN=120 NCCL=120 ./loadtests.sh
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
RESULTS="$ROOT/results"; mkdir -p "$RESULTS"

BASELINE="${BASELINE:-120}"
BURN="${BURN:-300}"
NCCL="${NCCL:-300}"
FIO_EACH="${FIO_EACH:-60}"
FIO_SIZE="${FIO_SIZE:-8G}"
FIO_DIR="${FIO_DIR:-$HOME/fio-scratch}"
COOLDOWN="${COOLDOWN:-60}"

DOCKER="docker"; docker info >/dev/null 2>&1 || DOCKER="sudo docker"
GPUS=$(nvidia-smi -L | wc -l)

# Pick a CUDA base image the installed driver supports.
DRV_CUDA=$(nvidia-smi | grep -oP 'CUDA Version: \K[0-9.]+' || echo 0)
if [ "$(printf '%s\n12.8\n' "$DRV_CUDA" | sort -V | head -1)" = "12.8" ]; then
  CUDA_TAG=12.8.2-devel-ubuntu22.04
else
  CUDA_TAG=12.4.1-devel-ubuntu22.04
fi

log "Building gpu-tools image (nccl-tests, gpu-burn) on CUDA $CUDA_TAG (driver supports CUDA $DRV_CUDA)"
$DOCKER build -q --build-arg CUDA_TAG="$CUDA_TAG" -t obsdemo/gpu-tools:latest "$ROOT/deploy/cloud/lambda/gpu-tools" >/dev/null
command -v fio >/dev/null || { log "Installing fio"; sudo apt-get install -y -qq fio >/dev/null; }

phase() { log "$1 ($2s)"; event "$1" start "$(attr duration_s "$2")"; }
done_phase() { event "$1" end "${2:-}"; }

# ----------------------------------------------------------------------------- baseline
phase baseline "$BASELINE"; sleep "$BASELINE"; done_phase baseline

# ----------------------------------------------------------------------------- gpu-burn
phase gpu-burn "$BURN"
$DOCKER run --rm --gpus all obsdemo/gpu-tools:latest gpu_burn -tc "$BURN" 2>&1 | tail -n 20 | tee "$RESULTS/gpu-burn.txt"
status=$(grep -qi 'FAULTY' "$RESULTS/gpu-burn.txt" && echo faulty || echo ok)
done_phase gpu-burn "$(attr result "$status")"

# ----------------------------------------------------------------------------- nccl all-reduce
if [ "$GPUS" -ge 2 ]; then
  phase nccl-allreduce "$NCCL"
  : > "$RESULTS/nccl-allreduce.txt"
  end=$((SECONDS + NCCL))
  while [ $SECONDS -lt $end ]; do
    $DOCKER run --rm --gpus all --ipc=host --ulimit memlock=-1 --ulimit stack=67108864 \
      obsdemo/gpu-tools:latest all_reduce_perf -b 8M -e 4G -f 2 -g "$GPUS" -n 20 \
      | tee -a "$RESULTS/nccl-allreduce.txt" | grep -E 'Avg bus bandwidth|^ +[0-9]+ ' | tail -n 3
  done
  busbw=$(grep -oP 'Avg bus bandwidth\s*:\s*\K[0-9.]+' "$RESULTS/nccl-allreduce.txt" | sort -g | tail -1)
  done_phase nccl-allreduce "$(attr nccl.avg_busbw_gbps "${busbw:-0}")$(attr gpus "$GPUS")"
else
  log "Skipping NCCL all-reduce: needs >= 2 GPUs (found $GPUS)"
fi

# ----------------------------------------------------------------------------- storage
phase storage-fio "$((FIO_EACH * 3))"
mkdir -p "$FIO_DIR"
for job in "seq-write:write:1M:16" "seq-read:read:1M:16" "rand-read-4k:randread:4k:64"; do
  IFS=: read -r name rw bs qd <<<"$job"
  event "storage-fio" "start-$name"
  fio --name="$name" --directory="$FIO_DIR" --rw="$rw" --bs="$bs" --iodepth="$qd" --numjobs=4 \
      --size="$FIO_SIZE" --runtime="$FIO_EACH" --time_based --direct=1 --ioengine=libaio \
      --group_reporting --output-format=json > "$RESULTS/fio-$name.json"
  read -r bw iops lat <<<"$(python3 - "$RESULTS/fio-$name.json" <<'PY'
import json, sys
j = json.load(open(sys.argv[1]))["jobs"][0]
side = j["read"] if j["read"]["io_bytes"] else j["write"]
print(round(side["bw_bytes"] / 1e6, 1), round(side["iops"]), round(side["clat_ns"]["mean"] / 1e3, 1))
PY
)"
  echo "  $name: ${bw} MB/s, ${iops} IOPS, mean latency ${lat} us"
  event "storage-fio" "end-$name" "$(attr fio.bw_mbps "$bw")$(attr fio.iops "$iops")$(attr fio.mean_lat_us "$lat")"
done
rm -rf "$FIO_DIR"
done_phase storage-fio

# ----------------------------------------------------------------------------- cooldown
phase cooldown "$COOLDOWN"; sleep "$COOLDOWN"; done_phase cooldown

log "Load tests finished. Results in $RESULTS. Next: ./deploy/cloud/lambda/export.sh"
