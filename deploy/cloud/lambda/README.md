# Lambda Cloud GPU run

Runs the same observability stack on a real Lambda GPU node (k3s instead of minikube) and adds
datacenter GPU telemetry: NVIDIA **dcgm-exporter** (SM/tensor activity, NVLink, PCIe, ECC, XID,
power) and **node_exporter** (InfiniBand port counters, NVMe/disks, filesystems). Everything still
lands in ClickHouse.

## Budget plan ($150 credits)

| Step | Instance | Time | Approx. cost |
|---|---|---|---|
| Dry run (shake out scripts) | 1× A10 / A6000 | ~1 h | ~$1 |
| Main run (NVLink, NCCL, 8-GPU DCGM) | 8× A100 SXM or 8× H100 SXM | 2–3 h | ~$30–90 |
| Reserve | | | ~$50 |

Prices are rough; check the Lambda console. **Billing runs until you terminate the instance** (idle
time counts), so export and terminate as soon as you're done.

## Runbook

### 0. On your laptop (free)
```powershell
.\scripts\package-for-cloud.ps1        # -> otel-observability.tgz (no build outputs)
```
Launch the instance in the Lambda console with your SSH key. Don't paste keys or API tokens anywhere.

### 1. Upload and bootstrap (~10–15 min)
```powershell
scp otel-observability.tgz ubuntu@<instance-ip>:~
ssh ubuntu@<instance-ip>
```
```bash
tar -xzf otel-observability.tgz && cd otel-observability
./deploy/cloud/lambda/bootstrap.sh
```
This writes `results/preflight.txt` (GPUs, NVLink topology, InfiniBand adapters, NVMe), installs
k3s + NVIDIA runtime + Helm, builds the full collector on the node, and deploys the chart with
`values-lambda.yaml`.

### 2. Open the UIs through an SSH tunnel
On the node: `./deploy/cloud/lambda/ui.sh &`
On your laptop (second terminal):
```powershell
ssh -N -L 3000:localhost:3000 -L 9090:localhost:9090 -L 8123:localhost:8123 ubuntu@<instance-ip>
```
Grafana: http://localhost:3000 (admin / admin) → **Cloud GPU node** dashboard.
Nothing is exposed on the instance's public IP; all services are ClusterIP.

### 3. Check data is arriving
```bash
./deploy/cloud/lambda/verify.sh
```
Expect families: *GPU device (DCGM)*, *GPU profiling (DCGM)*, *GPU NVLink (DCGM)* on SXM nodes,
*InfiniBand (node_exporter)* only if the node has HCAs, *Disk / NVMe*, *Kubernetes*, *Host*.

### 4. Load tests (~15 min with defaults)
```bash
./deploy/cloud/lambda/loadtests.sh
# shorter: BASELINE=60 BURN=120 NCCL=120 FIO_EACH=30 ./deploy/cloud/lambda/loadtests.sh
```
Phases: `baseline → gpu-burn → nccl-allreduce (≥2 GPUs) → storage-fio → cooldown`. Each phase is
an OTLP event (shown as dashboard annotations); NCCL bus bandwidth and fio MB/s / IOPS / latency are
attached to the events. Raw output lands in `results/`.

### 5. Export, copy, terminate
```bash
./deploy/cloud/lambda/export.sh          # -> ~/obs-run-<timestamp>.tar.gz (Parquet + results)
```
```powershell
scp ubuntu@<instance-ip>:~/obs-run-*.tar.gz .
```
**Terminate the instance in the Lambda console.**

### 6. Replay locally (free)
```powershell
.\scripts\import-run.ps1 -Archive .\obs-run-<timestamp>.tar.gz
```
Loads the run into database `lambda_run` in the minikube ClickHouse (no TTL). In Grafana, open
**Cloud GPU node** and switch **Database** to `lambda_run`, time range to the run window.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `dcgm-exporter` not Ready, logs mention profiling | Set `gpu.dcgmExporter.countersFile: /etc/dcgm-exporter/default-counters.csv` in `values-lambda.yaml`, re-run `helm upgrade` (loses SM/tensor/NVLink profiling fields). |
| `nvidia runtime not found in k3s containerd config` | `sudo systemctl restart k3s`, then re-run `bootstrap.sh`. |
| No InfiniBand data | Normal for single-node instances without HCAs; `results/preflight.txt` shows what exists. |
| Agent traces/metrics stuck after install | `kubectl -n observability rollout restart ds/obs-otel-agent` (bootstrap already does this once). |
