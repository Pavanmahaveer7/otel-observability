# GPU load-test tools: NVIDIA nccl-tests (collective bandwidth over NVLink/PCIe/IB)
# and gpu-burn (sustained compute stress). Built on the node by loadtests.sh.
ARG CUDA_TAG=12.8.2-devel-ubuntu22.04
FROM nvidia/cuda:${CUDA_TAG}
RUN apt-get update \
 && apt-get install -y --no-install-recommends git make ca-certificates libnccl2 libnccl-dev \
 && rm -rf /var/lib/apt/lists/*
RUN git clone --depth 1 https://github.com/NVIDIA/nccl-tests /opt/nccl-tests \
 && make -C /opt/nccl-tests -j"$(nproc)"
RUN git clone --depth 1 https://github.com/wilicc/gpu-burn /opt/gpu-burn \
 && make -C /opt/gpu-burn
ENV PATH=/opt/nccl-tests/build:/opt/gpu-burn:$PATH
WORKDIR /opt/gpu-burn
