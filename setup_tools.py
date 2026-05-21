#!/usr/bin/env python3
"""
setup_tools.py
--------------
Automatically downloads and installs ALL EDA tools needed for TacoMAS-VLSI:
  - OSS-CAD-Suite (Yosys, Verilator, Icarus Verilog, SymbiYosys, Magic,
                   Netgen, OpenSTA, nextpnr, cocotb, gtkwave, eqy, sby ...)
  - OpenLane + OpenROAD (via pip / Docker)
  - Sky130 PDK (via volare)
  - Python dependencies

Run once before starting the orchestrator:
  python setup_tools.py
"""

import os
import sys
import platform
import subprocess
import urllib.request
import tarfile
import json
from pathlib import Path
from datetime import datetime

# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────
INSTALL_DIR   = Path.home() / "oss-cad-suite"
PDK_ROOT      = Path.home() / "pdk"
SKY130_PDK    = PDK_ROOT / "sky130"
LOG_FILE      = Path("setup_tools.log")

OSS_CAD_BASE  = "https://github.com/YosysHQ/oss-cad-suite-build/releases/download"
OSS_CAD_DATE  = "2025-03-08"
ARCH_MAP = {
    ("Linux",  "x86_64"):  f"oss-cad-suite-linux-x64-{OSS_CAD_DATE.replace('-','')}.tgz",
    ("Linux",  "aarch64"): f"oss-cad-suite-linux-arm64-{OSS_CAD_DATE.replace('-','')}.tgz",
    ("Darwin", "x86_64"):  f"oss-cad-suite-darwin-x64-{OSS_CAD_DATE.replace('-','')}.tgz",
    ("Darwin", "arm64"):   f"oss-cad-suite-darwin-arm64-{OSS_CAD_DATE.replace('-','')}.tgz",
}

# ─────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────
def log(msg: str):
    ts = datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")

def run(cmd: list, check=True, env=None) -> subprocess.CompletedProcess:
    log(f"  $ {' '.join(str(c) for c in cmd)}")
    result = subprocess.run(cmd, capture_output=True, text=True, env=env)
    if result.stdout.strip():
        log(f"    stdout: {result.stdout.strip()[:200]}")
    if result.returncode != 0 and check:
        log(f"  ERROR: {result.stderr.strip()[:400]}")
        raise RuntimeError(f"Command failed: {' '.join(str(c) for c in cmd)}")
    return result

def tool_exists(name: str) -> bool:
    result = subprocess.run(["which", name], capture_output=True)
    return result.returncode == 0

def write_env_script(oss_dir: Path, pdk_root: Path):
    env_content = f"""#!/bin/bash
# TacoMAS-VLSI Environment Setup
# Run: source env.sh

# OSS-CAD-Suite
source {oss_dir}/environment

# PDK
export PDK_ROOT={pdk_root}
export PDK=sky130A
export PDKPATH={pdk_root}/sky130A

# Verify key tools
echo "✓ Yosys:      $(yosys --version 2>&1 | head -1)"
echo "✓ Verilator:  $(verilator --version 2>&1 | head -1)"
echo "✓ Icarus:     $(iverilog -V 2>&1 | head -1)"
echo "✓ SymbiYosys: $(sby --version 2>&1 | head -1)"
echo "✓ Magic:      $(magic --version 2>&1 | head -1)"
echo "✓ OpenSTA:    $(sta -version 2>&1 | head -1)"
echo "✓ PDK:        $PDK_ROOT"
echo ""
echo "TacoMAS-VLSI environment ready."
"""
    with open("env.sh", "w") as f:
        f.write(env_content)
    os.chmod("env.sh", 0o755)
    log("  env.sh written — run: source env.sh")


# ─────────────────────────────────────────────
# STEP 1 — OSS-CAD-SUITE
# ─────────────────────────────────────────────
def install_oss_cad_suite():
    log("\n══════════════════════════════════════")
    log("STEP 1: OSS-CAD-Suite")
    log("══════════════════════════════════════")

    if (INSTALL_DIR / "bin" / "yosys").exists():
        log("  ✓ OSS-CAD-Suite already installed at " + str(INSTALL_DIR))
        return

    system  = platform.system()
    machine = platform.machine()
    key     = (system, machine)

    if key not in ARCH_MAP:
        raise RuntimeError(f"Unsupported platform: {system} {machine}")

    filename = ARCH_MAP[key]
    url      = f"{OSS_CAD_BASE}/{OSS_CAD_DATE}/{filename}"
    dest     = Path("/tmp") / filename

    log(f"  Downloading: {url}")
    log(f"  → {dest}  (~1GB, may take 2-5 minutes)")

    def progress(block_num, block_size, total_size):
        downloaded = block_num * block_size
        pct = min(100, downloaded * 100 // total_size) if total_size > 0 else 0
        if block_num % 500 == 0:
            log(f"    {pct}%  ({downloaded // 1024 // 1024} MB / "
                f"{total_size // 1024 // 1024} MB)")

    urllib.request.urlretrieve(url, dest, reporthook=progress)
    log("  Download complete. Extracting...")

    INSTALL_DIR.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(dest, "r:gz") as tar:
        tar.extractall(INSTALL_DIR.parent)

    log(f"  ✓ OSS-CAD-Suite installed at {INSTALL_DIR}")
    dest.unlink()
    log("  Tarball removed.")


# ─────────────────────────────────────────────
# STEP 2 — SKY130 PDK via volare
# ─────────────────────────────────────────────
def install_sky130_pdk():
    log("\n══════════════════════════════════════")
    log("STEP 2: Sky130 PDK via volare")
    log("══════════════════════════════════════")

    if (SKY130_PDK / "sky130A").exists():
        log(f"  ✓ Sky130 PDK already at {SKY130_PDK}")
        return

    run([sys.executable, "-m", "pip", "install", "volare", "--quiet"])
    log("  ✓ volare installed")

    PDK_ROOT.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["PDK_ROOT"] = str(PDK_ROOT)

    log("  Downloading sky130A PDK (~500MB, 5-10 minutes)...")
    run(
        [sys.executable, "-m", "volare", "enable",
         "--pdk", "sky130",
         "--pdk-root", str(PDK_ROOT),
         "bdc9412b3e468c102d01b7cf6337be06ec6e9c9a"],
        env=env
    )
    log(f"  ✓ Sky130 PDK installed at {PDK_ROOT}")


# ─────────────────────────────────────────────
# STEP 3 — OpenLane2
# ─────────────────────────────────────────────
def install_openlane():
    log("\n══════════════════════════════════════")
    log("STEP 3: OpenLane2")
    log("══════════════════════════════════════")

    result = subprocess.run(
        [sys.executable, "-c", "import openlane; print(openlane.__version__)"],
        capture_output=True, text=True
    )
    if result.returncode == 0:
        log(f"  ✓ OpenLane2 already installed: v{result.stdout.strip()}")
        return

    run([sys.executable, "-m", "pip", "install", "openlane", "--quiet"])
    log("  ✓ OpenLane2 installed")


# ─────────────────────────────────────────────
# STEP 4 — Python dependencies
# ─────────────────────────────────────────────
def install_python_deps():
    log("\n══════════════════════════════════════")
    log("STEP 4: Python dependencies")
    log("══════════════════════════════════════")

    packages = [
        "openai>=1.0.0",
        "vllm>=0.4.0",
        "cocotb>=1.9.0",
        "pyDigitalWaveTools",
        "pyyaml",
        "rich",
        "tqdm",
    ]
    run([sys.executable, "-m", "pip", "install"] + packages + ["--quiet"])
    log("  ✓ Python packages installed")


# ─────────────────────────────────────────────
# STEP 5 — Verify tools
# ─────────────────────────────────────────────
REQUIRED_TOOLS = {
    "yosys":     "Synthesis (RTL → netlist)",
    "verilator": "Lint + fast simulation",
    "iverilog":  "Functional simulation",
    "vvp":       "Icarus simulation runner",
    "sby":       "SymbiYosys formal verification",
    "sta":       "OpenSTA static timing analysis",
    "magic":     "DRC / LVS / parasitic extraction",
    "netgen":    "LVS netlist comparison",
    "eqy":       "Logic equivalence checking",
    "openroad":  "Place & route",
    "klayout":   "Layout viewer + DRC",
}

def verify_tools():
    log("\n══════════════════════════════════════")
    log("STEP 5: Tool verification")
    log("══════════════════════════════════════")

    env_file = INSTALL_DIR / "environment"
    status = {}
    all_ok = True

    for tool, desc in REQUIRED_TOOLS.items():
        result = subprocess.run(
            f"source {env_file} && which {tool}",
            shell=True, capture_output=True, text=True,
            executable="/bin/bash"
        )
        found = result.returncode == 0
        status[tool] = found
        icon = "✓" if found else "✗"
        log(f"  {icon} {tool:12s} — {desc}")
        if not found:
            all_ok = False

    if all_ok:
        log("\n  ✓ ALL TOOLS VERIFIED")
    else:
        missing = [t for t, ok in status.items() if not ok]
        log(f"\n  ⚠ Missing tools: {missing}")

    return status


# ─────────────────────────────────────────────
# STEP 6 — vLLM startup script
# ─────────────────────────────────────────────
def write_vllm_startup():
    log("\n══════════════════════════════════════")
    log("STEP 6: vLLM server startup scripts")
    log("══════════════════════════════════════")

    script = """#!/bin/bash
# start_vllm_servers.sh — starts 5 vLLM servers on 8x AMD MI300X

set -e
echo "Starting TacoMAS-VLSI vLLM servers..."

# GPU 0-1: Fast agents
CUDA_VISIBLE_DEVICES=0,1 vllm serve Qwen/Qwen3-Coder-30B-Instruct \\
  --tensor-parallel-size 2 --port 8000 \\
  --max-model-len 32768 --dtype bfloat16 \\
  --gpu-memory-utilization 0.90 &
echo "  [GPU 0-1] agent_fast → port 8000"
sleep 5

# GPU 2-3: Verification agents
CUDA_VISIBLE_DEVICES=2,3 vllm serve Qwen/Qwen3-Coder-30B-Instruct \\
  --tensor-parallel-size 2 --port 8001 \\
  --max-model-len 32768 --dtype bfloat16 \\
  --gpu-memory-utilization 0.90 &
echo "  [GPU 2-3] agent_verif → port 8001"
sleep 5

# GPU 4-5: Physical design agents
CUDA_VISIBLE_DEVICES=4,5 vllm serve Qwen/Qwen3-Coder-30B-Instruct \\
  --tensor-parallel-size 2 --port 8002 \\
  --max-model-len 32768 --dtype bfloat16 \\
  --gpu-memory-utilization 0.90 &
echo "  [GPU 4-5] agent_pd → port 8002"
sleep 5

# GPU 6: Meta-judge
CUDA_VISIBLE_DEVICES=6 vllm serve Qwen/Qwen3-14B-Instruct \\
  --tensor-parallel-size 1 --port 8003 \\
  --max-model-len 16384 --dtype bfloat16 \\
  --gpu-memory-utilization 0.85 &
echo "  [GPU 6]   meta_judge → port 8003"
sleep 5

# GPU 7: Meta-topology evolver
CUDA_VISIBLE_DEVICES=7 vllm serve deepseek-ai/DeepSeek-R1-Distill-Qwen-32B \\
  --tensor-parallel-size 1 --port 8004 \\
  --max-model-len 32768 --dtype bfloat16 \\
  --gpu-memory-utilization 0.90 &
echo "  [GPU 7]   meta_topology → port 8004"

echo ""
echo "Waiting 60s for all servers to load models..."
sleep 60

echo "Health check:"
for port in 8000 8001 8002 8003 8004; do
  curl -s http://localhost:${port}/health > /dev/null && \\
    echo "  ✓ port ${port} ready" || \\
    echo "  ✗ port ${port} not ready"
done

echo ""
echo "Run: source env.sh && python tacomas_vlsi_orchestrator.py"
"""
    with open("start_vllm_servers.sh", "w") as f:
        f.write(script)
    os.chmod("start_vllm_servers.sh", 0o755)
    log("  ✓ start_vllm_servers.sh written")


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────
def main():
    log("TacoMAS-VLSI Tool Setup")
    log(f"Platform: {platform.system()} {platform.machine()}")
    log(f"Install dir: {INSTALL_DIR}")
    log(f"PDK root:    {PDK_ROOT}")

    install_oss_cad_suite()
    install_sky130_pdk()
    install_openlane()
    install_python_deps()
    tool_status = verify_tools()
    write_env_script(INSTALL_DIR, PDK_ROOT)
    write_vllm_startup()

    log("\n══════════════════════════════════════")
    log("SETUP COMPLETE")
    log("══════════════════════════════════════")
    log("Next steps:")
    log("  1. source env.sh")
    log("  2. bash start_vllm_servers.sh")
    log("  3. python tacomas_vlsi_orchestrator.py")

    with open("setup_status.json", "w") as f:
        json.dump({
            "oss_cad_suite": str(INSTALL_DIR),
            "pdk_root": str(PDK_ROOT),
            "tools": tool_status,
            "timestamp": datetime.now().isoformat(),
        }, f, indent=2)


if __name__ == "__main__":
    main()
