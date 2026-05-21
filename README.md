# TacoMAS-VLSI

**Test-Time Co-Evolution of Topology and Capability for RTL-to-GDSII Pipeline**

An adaptive multi-agent orchestrator for 30-stage ASIC/VLSI chip design automation, built on the TacoMAS framework with EDA tool verifiers and automatic synthetic data generation.

## Architecture

- **Fast Loop** — Meta-judge scores each stage agent after every run and writes refinement signals back into agent memory (capability update)
- **Slow Loop** — Meta-LLM reviews full trajectories and evolves the agent graph topology (birth/death/rewire) with hard budgets |ΔV| ≤ 2, |ΔE| ≤ 4
- **EDA Verifiers** — Verilator, Icarus Verilog, SymbiYosys, OpenSTA, Magic DRC as deterministic reward signals
- **Data Sink** — Every verified stage output auto-writes to SFT / RLVR / Topology JSONL datasets

## Pipeline Stages

Spec → RTL → Verification → Synthesis & DFT → Physical Design → Signoff (GDSII)

## Hardware Setup (8× AMD MI300X)

| GPUs | Role | Model |
|---|---|---|
| 0–1 | Fast agents (Spec, RTL, Synth) | Qwen3-Coder-30B-Instruct |
| 2–3 | Verification agents | Qwen3-Coder-30B-Instruct |
| 4–5 | Physical design agents | Qwen3-Coder-30B-Instruct |
| 6 | Meta-judge (fast loop) | Qwen3-14B-Instruct |
| 7 | Meta-LLM topology evolver (slow loop) | DeepSeek-R1-Distill-Qwen-32B |

## Quick Start

```bash
# Start vLLM servers
CUDA_VISIBLE_DEVICES=0,1 vllm serve Qwen/Qwen3-Coder-30B-Instruct --tensor-parallel-size 2 --port 8000 &
CUDA_VISIBLE_DEVICES=2,3 vllm serve Qwen/Qwen3-Coder-30B-Instruct --tensor-parallel-size 2 --port 8001 &
CUDA_VISIBLE_DEVICES=4,5 vllm serve Qwen/Qwen3-Coder-30B-Instruct --tensor-parallel-size 2 --port 8002 &
CUDA_VISIBLE_DEVICES=6   vllm serve Qwen/Qwen3-14B-Instruct --port 8003 &
CUDA_VISIBLE_DEVICES=7   vllm serve deepseek-ai/DeepSeek-R1-Distill-Qwen-32B --port 8004 &

# Run orchestrator
python tacomas_vlsi_orchestrator.py
```

## Scale Data Generation

```python
from tacomas_vlsi_orchestrator import run_scale

BASE_DESIGN = {
    "design_name": "uart_ctrl",
    "pdk": "sky130",
    "top_module": "uart_ctrl",
    "clock_mhz": 100,
    "bus_width": 8,
}

# 100 variants × N seeds = millions of verified training records
run_scale(BASE_DESIGN, n_variants=100, output_dir="./synthetic_data")
```

## References

- [TacoMAS Paper (arXiv 2605.09539)](https://arxiv.org/abs/2605.09539)
- [CodeV-R1: Reasoning-Enhanced Verilog Generation](https://arxiv.org/abs/2505.24183)
- [OpenLane/OpenROAD](https://github.com/The-OpenROAD-Project/OpenLane)
