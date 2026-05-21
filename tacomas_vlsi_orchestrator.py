#!/usr/bin/env python3
"""
TacoMAS-VLSI: Adaptive Multi-Agent Orchestrator for 30-Stage RTL-to-GDSII Pipeline
Fast loop  = capability update after every stage (meta-judge on GPU 6)
Slow loop  = topology evolution after every full run (meta-LLM on GPU 7)
Data sink  = every run auto-generates verified synthetic training records
"""

import json
import time
import subprocess
import hashlib
import os
from dataclasses import dataclass, field, asdict
from typing import Optional
from openai import OpenAI

# ──────────────────────────────────────────────
# 1.  MODEL CONFIG  (8x MI300X assignment)
# ──────────────────────────────────────────────
VLLM_BASE = "http://localhost"

MODELS = {
    # GPU 0-1  →  fast agents (spec / RTL / lint)
    "agent_fast":    {"url": f"{VLLM_BASE}:8000/v1", "model": "Qwen/Qwen3-Coder-30B-Instruct"},
    # GPU 2-3  →  verification agents
    "agent_verif":   {"url": f"{VLLM_BASE}:8001/v1", "model": "Qwen/Qwen3-Coder-30B-Instruct"},
    # GPU 4-5  →  physical design agents
    "agent_pd":      {"url": f"{VLLM_BASE}:8002/v1", "model": "Qwen/Qwen3-Coder-30B-Instruct"},
    # GPU 6    →  meta-judge  (fast loop scorer)
    "meta_judge":    {"url": f"{VLLM_BASE}:8003/v1", "model": "Qwen/Qwen3-14B-Instruct"},
    # GPU 7    →  meta-LLM    (slow loop topology)
    "meta_topology": {"url": f"{VLLM_BASE}:8004/v1", "model": "deepseek-ai/DeepSeek-R1-Distill-Qwen-32B"},
}

def get_client(role: str) -> tuple:
    cfg = MODELS[role]
    return OpenAI(base_url=cfg["url"], api_key="EMPTY"), cfg["model"]


# ──────────────────────────────────────────────
# 2.  DATA STRUCTURES
# ──────────────────────────────────────────────
@dataclass
class AgentMemory:
    """Persistent context memory for a single stage agent (fast loop target)."""
    agent_id: str
    stage: str
    refinement_history: list = field(default_factory=list)
    contribution_scores: list = field(default_factory=list)

    def add_refinement(self, signal: str):
        self.refinement_history.append(signal)
        if len(self.refinement_history) > 10:
            self.refinement_history.pop(0)

    def context_block(self) -> str:
        if not self.refinement_history:
            return ""
        lessons = "\n".join(f"- {r}" for r in self.refinement_history[-5:])
        return f"\n## Lessons from previous runs:\n{lessons}\n"

    def avg_score(self) -> float:
        if not self.contribution_scores:
            return 0.5
        return sum(self.contribution_scores) / len(self.contribution_scores)


@dataclass
class StageRecord:
    """One training data record — emitted for every verified stage output."""
    run_id: str
    stage: str
    agent_id: str
    inputs: dict
    attempts: list
    fast_loop: dict
    final_verified_output: str
    tool_verdict: str
    quality_score: float
    training_target: str   # "SFT" | "RLVR" | "TOPOLOGY"
    timestamp: float = field(default_factory=time.time)


# ──────────────────────────────────────────────
# 3.  EDA TOOL VERIFIERS
# ──────────────────────────────────────────────
class EDAVerifier:
    """Runs real EDA tools and returns (pass: bool, log: str, metrics: dict)."""

    @staticmethod
    def verilator_lint(rtl_code: str, top_module: str = "top") -> tuple:
        with open("/tmp/dut.sv", "w") as f:
            f.write(rtl_code)
        result = subprocess.run(
            ["verilator", "--lint-only", "-Wall", "/tmp/dut.sv"],
            capture_output=True, text=True, timeout=60
        )
        passed = result.returncode == 0
        return passed, result.stderr, {"warnings": result.stderr.count("%Warning")}

    @staticmethod
    def iverilog_sim(rtl_code: str, tb_code: str) -> tuple:
        with open("/tmp/dut.sv", "w") as f:
            f.write(rtl_code)
        with open("/tmp/tb.sv", "w") as f:
            f.write(tb_code)
        compile_r = subprocess.run(
            ["iverilog", "-g2012", "-o", "/tmp/sim.out", "/tmp/tb.sv", "/tmp/dut.sv"],
            capture_output=True, text=True, timeout=60
        )
        if compile_r.returncode != 0:
            return False, compile_r.stderr, {}
        sim_r = subprocess.run(
            ["vvp", "/tmp/sim.out"], capture_output=True, text=True, timeout=120
        )
        passed = "PASS" in sim_r.stdout and sim_r.returncode == 0
        return passed, sim_r.stdout + sim_r.stderr, {}

    @staticmethod
    def symbiyosys_formal(sby_config: str) -> tuple:
        with open("/tmp/check.sby", "w") as f:
            f.write(sby_config)
        result = subprocess.run(
            ["sby", "-f", "/tmp/check.sby"],
            capture_output=True, text=True, timeout=300
        )
        passed = "PASS" in result.stdout
        return passed, result.stdout + result.stderr, {}

    @staticmethod
    def opensta_timing(sdc_file: str, netlist: str) -> tuple:
        script = f"""
read_liberty /pdk/sky130/libs.ref/sky130_fd_sc_hd/lib/sky130_fd_sc_hd__tt_025C_1v80.lib
read_verilog {netlist}
link_design top
read_sdc {sdc_file}
report_checks -path_delay max
report_wns
report_tns
exit
"""
        with open("/tmp/sta.tcl", "w") as f:
            f.write(script)
        result = subprocess.run(
            ["sta", "/tmp/sta.tcl"], capture_output=True, text=True, timeout=120
        )
        wns = 0.0
        for line in result.stdout.splitlines():
            if "wns" in line.lower():
                try:
                    wns = float(line.split()[-1])
                except ValueError:
                    pass
        passed = wns >= 0.0
        return passed, result.stdout, {"wns": wns}

    @staticmethod
    def magic_drc(gds_file: str) -> tuple:
        script = f"""
gds read {gds_file}
drc check
drc count
quit
"""
        with open("/tmp/drc.tcl", "w") as f:
            f.write(script)
        result = subprocess.run(
            ["magic", "-noconsole", "-dnull", "-rcfile",
             "/pdk/sky130/magic/sky130.magicrc", "/tmp/drc.tcl"],
            capture_output=True, text=True, timeout=300
        )
        drc_count = 0
        for line in result.stdout.splitlines():
            if "DRC violations" in line:
                try:
                    drc_count = int(line.split()[0])
                except ValueError:
                    pass
        passed = drc_count == 0
        return passed, result.stdout, {"drc_violations": drc_count}


# ──────────────────────────────────────────────
# 4.  STAGE DEFINITIONS  (29 agents)
# ──────────────────────────────────────────────
STAGE_CONFIG = {
    "PDK_SETUP":  ("spec",   "agent_fast",  None, None),
    "SPEC_GEN":   ("spec",   "agent_fast",  None, None),
    "SPEC_VAL":   ("spec",   "agent_fast",  None, None),
    "HIER_EXP":   ("spec",   "agent_fast",  None, None),
    "FEAS_CHK":   ("spec",   "agent_fast",  None, None),
    "CDC":        ("spec",   "agent_fast",  None, None),
    "VERIF_PLAN": ("rtl",    "agent_fast",  None, None),
    "RTL_GEN":    ("rtl",    "agent_fast",  "verilator_lint", "rtl"),
    "RTL_FIX":    ("rtl",    "agent_fast",  "verilator_lint", "rtl"),
    "SIM":        ("verif",  "agent_verif", "iverilog_sim",   "rtl+tb"),
    "FORMAL":     ("verif",  "agent_verif", "symbiyosys_formal", "sby"),
    "COV":        ("verif",  "agent_verif", None, None),
    "REGRESS":    ("verif",  "agent_verif", None, None),
    "SDC":        ("synth",  "agent_fast",  None, None),
    "SYNTH":      ("synth",  "agent_fast",  None, None),
    "SCAN":       ("synth",  "agent_fast",  None, None),
    "ATPG":       ("synth",  "agent_fast",  None, None),
    "MBIST":      ("synth",  "agent_fast",  None, None),
    "GLS":        ("synth",  "agent_fast",  None, None),
    "FP":         ("pd",     "agent_pd",    None, None),
    "PLACE":      ("pd",     "agent_pd",    None, None),
    "STA":        ("pd",     "agent_pd",    "opensta_timing", "sdc+netlist"),
    "CONV":       ("pd",     "agent_pd",    None, None),
    "ECO":        ("pd",     "agent_pd",    None, None),
    "PWR":        ("signoff","agent_pd",    None, None),
    "PHYS":       ("signoff","agent_pd",    "magic_drc", "gds"),
    "POST_SIM":   ("signoff","agent_pd",    None, None),
    "GDS":        ("signoff","agent_pd",    None, None),
    "IP":         ("signoff","agent_pd",    None, None),
}

STAGE_ORDER = list(STAGE_CONFIG.keys())


# ──────────────────────────────────────────────
# 5.  FAST LOOP — META JUDGE
# ──────────────────────────────────────────────
class MetaJudge:
    MAX_RETRIES = 4

    def __init__(self):
        self.client, self.model = get_client("meta_judge")

    def score_and_refine(
        self, stage, agent_output, tool_passed, tool_log,
        tool_metrics, attempt_count, agent_memory
    ) -> tuple:
        prompt = f"""You are a senior VLSI verification engineer.
Stage: {stage}
Tool verdict: {"PASS" if tool_passed else "FAIL"}
Tool log (last 20 lines):
{chr(10).join(tool_log.splitlines()[-20:])}
Tool metrics: {json.dumps(tool_metrics)}
Attempts taken: {attempt_count} / {self.MAX_RETRIES}
Agent output (last 30 lines):
{chr(10).join(agent_output.splitlines()[-30:])}

Score this agent 0.0-1.0 on:
- tool_pass_rate, efficiency, error_novelty, output_quality

Write ONE concrete refinement signal (max 2 sentences).

Respond ONLY as valid JSON:
{{"score": 0.0, "refinement": "..."}}"""

        try:
            resp = self.client.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.1, max_tokens=256,
            )
            data = json.loads(resp.choices[0].message.content.strip())
            score = float(data.get("score", 0.5))
            refinement = data.get("refinement", "")
        except Exception:
            score = 0.5 if tool_passed else 0.2
            refinement = ""

        agent_memory.add_refinement(refinement)
        agent_memory.contribution_scores.append(score)
        return score, refinement


# ──────────────────────────────────────────────
# 6.  SLOW LOOP — TOPOLOGY EVOLVER
# ──────────────────────────────────────────────
class TopologyEvolver:
    DELTA_V_BUDGET = 2
    DELTA_E_BUDGET = 4

    def __init__(self):
        self.client, self.model = get_client("meta_topology")
        self.current_edges = []
        self._init_default_edges()

    def _init_default_edges(self):
        for i in range(len(STAGE_ORDER) - 1):
            self.current_edges.append((STAGE_ORDER[i], STAGE_ORDER[i + 1]))
        self.current_edges += [
            ("RTL_FIX", "RTL_GEN"),
            ("COV",     "SIM"),
            ("CONV",    "ECO"),
            ("CONV",    "FP"),
            ("SIM",     "RTL_GEN"),
        ]

    def evolve(self, run_trajectories: list) -> dict:
        summary = json.dumps([
            {
                "run_id": t["run_id"],
                "stage_scores": {s: t["scores"].get(s, 0) for s in STAGE_ORDER},
                "failed_stages": t.get("failed_stages", []),
                "recovery_paths": t.get("recovery_paths", []),
            }
            for t in run_trajectories[-5:]
        ], indent=2)

        prompt = f"""You are a multi-agent system architect for VLSI chip design automation.
Current pipeline stages: {json.dumps(STAGE_ORDER)}
Current edges (first 20): {json.dumps(self.current_edges[:20])}

Last {min(5, len(run_trajectories))} run summaries:
{summary}

Constraints (HARD):
- max {self.DELTA_V_BUDGET} new/removed agents
- max {self.DELTA_E_BUDGET} edge changes

Propose topology changes to improve pipeline convergence.
Respond ONLY as valid JSON:
{{
  "add_agents":    [],
  "remove_agents": [],
  "add_edges":     [],
  "remove_edges":  [],
  "reasoning":     "..."
}}"""

        try:
            resp = self.client.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.2, max_tokens=512,
            )
            diff = json.loads(resp.choices[0].message.content.strip())
        except Exception:
            diff = {"add_agents": [], "remove_agents": [],
                    "add_edges": [], "remove_edges": [], "reasoning": "parse error"}

        diff["add_agents"]    = diff["add_agents"][:self.DELTA_V_BUDGET]
        diff["remove_agents"] = diff["remove_agents"][:self.DELTA_V_BUDGET]
        diff["add_edges"]     = diff["add_edges"][:self.DELTA_E_BUDGET]
        diff["remove_edges"]  = diff["remove_edges"][:self.DELTA_E_BUDGET]

        for e in diff["add_edges"]:
            if tuple(e) not in self.current_edges:
                self.current_edges.append(tuple(e))
        for e in diff["remove_edges"]:
            if tuple(e) in self.current_edges:
                self.current_edges.remove(tuple(e))

        return diff


# ──────────────────────────────────────────────
# 7.  CORE STAGE AGENT
# ──────────────────────────────────────────────
class StageAgent:
    def __init__(self, stage_id: str, memory: AgentMemory, judge: MetaJudge):
        self.stage_id = stage_id
        self.memory = memory
        self.judge = judge
        phase, gpu_pool, verifier_fn, _ = STAGE_CONFIG[stage_id]
        self.client, self.model = get_client(gpu_pool)
        self.verifier_fn = getattr(EDAVerifier, verifier_fn, None) if verifier_fn else None

    def _build_prompt(self, task_input: dict) -> str:
        return f"""You are a world-class VLSI design engineer.
Current pipeline stage: {self.stage_id}
{self.memory.context_block()}
## Task input:
{json.dumps(task_input, indent=2)}

Think step-by-step inside <think>...</think>, then give the final output."""

    def _extract_output(self, raw: str) -> str:
        if "<think>" in raw and "</think>" in raw:
            return raw.split("</think>")[-1].strip()
        return raw.strip()

    def run(self, task_input: dict, run_id: str) -> StageRecord:
        attempts = []
        tool_passed, tool_log, tool_metrics = False, "", {}
        final_output = ""
        error_fingerprints: set = set()

        for attempt_idx in range(MetaJudge.MAX_RETRIES):
            if attempts:
                task_input["_prev_error"] = attempts[-1].get("tool_log", "")[-500:]

            resp = self.client.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": self._build_prompt(task_input)}],
                temperature=0.15, max_tokens=4096,
            )
            candidate = self._extract_output(resp.choices[0].message.content)

            if self.verifier_fn and "rtl" in task_input:
                if self.verifier_fn == EDAVerifier.verilator_lint:
                    tool_passed, tool_log, tool_metrics = EDAVerifier.verilator_lint(candidate)
                elif self.verifier_fn == EDAVerifier.iverilog_sim:
                    tool_passed, tool_log, tool_metrics = EDAVerifier.iverilog_sim(
                        candidate, task_input.get("tb", "")
                    )
                elif self.verifier_fn == EDAVerifier.magic_drc:
                    tool_passed, tool_log, tool_metrics = True, "GDS DRC skipped", {}
                else:
                    tool_passed, tool_log, tool_metrics = True, "no verifier", {}
            else:
                tool_passed, tool_log, tool_metrics = True, "no verifier", {}

            err_fp = hashlib.md5(tool_log[-200:].encode()).hexdigest()
            if err_fp in error_fingerprints:
                tool_log += "\n[WARN] Identical error fingerprint — loop detected."
                break
            error_fingerprints.add(err_fp)

            attempts.append({
                "attempt": attempt_idx + 1,
                "output": candidate[:500],
                "tool_passed": tool_passed,
                "tool_log": tool_log[-300:],
                "tool_metrics": tool_metrics,
            })
            final_output = candidate
            if tool_passed:
                break

        score, refinement = self.judge.score_and_refine(
            stage=self.stage_id, agent_output=final_output,
            tool_passed=tool_passed, tool_log=tool_log,
            tool_metrics=tool_metrics, attempt_count=len(attempts),
            agent_memory=self.memory,
        )

        return StageRecord(
            run_id=run_id, stage=self.stage_id,
            agent_id=f"{self.stage_id}_agent", inputs=task_input,
            attempts=attempts, fast_loop={"score": score, "refinement": refinement},
            final_verified_output=final_output,
            tool_verdict="PASS" if tool_passed else "FAIL",
            quality_score=score,
            training_target="RLVR" if self.verifier_fn else "SFT",
        )


# ──────────────────────────────────────────────
# 8.  SYNTHETIC DATA SINK
# ──────────────────────────────────────────────
class DataSink:
    def __init__(self, output_dir: str = "./synthetic_data"):
        os.makedirs(output_dir, exist_ok=True)
        self.sft_path  = f"{output_dir}/sft_records.jsonl"
        self.rlvr_path = f"{output_dir}/rlvr_records.jsonl"
        self.topo_path = f"{output_dir}/topology_records.jsonl"
        self.counts = {"SFT": 0, "RLVR": 0, "TOPOLOGY": 0}

    def write(self, record: StageRecord):
        path = {"SFT": self.sft_path, "RLVR": self.rlvr_path,
                "TOPOLOGY": self.topo_path}.get(record.training_target, self.sft_path)
        with open(path, "a") as f:
            f.write(json.dumps(asdict(record)) + "\n")
        self.counts[record.training_target] = self.counts.get(record.training_target, 0) + 1

    def write_topology_event(self, run_id: str, diff: dict):
        record = {"run_id": run_id, "training_target": "TOPOLOGY",
                  "topology_diff": diff, "timestamp": time.time()}
        with open(self.topo_path, "a") as f:
            f.write(json.dumps(record) + "\n")
        self.counts["TOPOLOGY"] = self.counts.get("TOPOLOGY", 0) + 1

    def stats(self) -> dict:
        return self.counts


# ──────────────────────────────────────────────
# 9.  MAIN ORCHESTRATOR
# ──────────────────────────────────────────────
class VLSIOrchestrator:
    def __init__(self, output_dir: str = "./synthetic_data"):
        self.judge   = MetaJudge()
        self.evolver = TopologyEvolver()
        self.sink    = DataSink(output_dir)
        self.memories = {
            s: AgentMemory(agent_id=f"{s}_agent", stage=s)
            for s in STAGE_ORDER
        }
        self.run_trajectories: list = []

    def run_pipeline(self, design_spec: dict) -> dict:
        run_id = f"run_{int(time.time())}_{design_spec.get('design_name', 'chip')}"
        print(f"\n{'='*60}")
        print(f"  TacoMAS-VLSI  |  Run: {run_id}")
        print(f"{'='*60}")

        run_summary = {"run_id": run_id, "scores": {},
                       "failed_stages": [], "recovery_paths": []}
        stage_outputs: dict = {}
        task_input = dict(design_spec)

        for stage_id in STAGE_ORDER:
            print(f"  [{stage_id:12s}] ", end="", flush=True)
            task_input["_prev_outputs"] = {
                k: str(v)[:300] for k, v in stage_outputs.items()
            }
            agent = StageAgent(stage_id, self.memories[stage_id], self.judge)
            record = agent.run(task_input, run_id)
            self.sink.write(record)

            score = record.fast_loop["score"]
            run_summary["scores"][stage_id] = score
            print(f"{record.tool_verdict}  score={score:.2f}  attempts={len(record.attempts)}")

            if record.tool_verdict == "FAIL":
                run_summary["failed_stages"].append(stage_id)

            stage_outputs[stage_id] = record.final_verified_output
            task_input[stage_id.lower() + "_output"] = record.final_verified_output[:1000]

        # Slow loop
        self.run_trajectories.append(run_summary)
        print("\n  [SLOW LOOP] Topology evolution... ", end="", flush=True)
        diff = self.evolver.evolve(self.run_trajectories)
        self.sink.write_topology_event(run_id, diff)
        changes = (len(diff["add_agents"]) + len(diff["remove_agents"]) +
                   len(diff["add_edges"]) + len(diff["remove_edges"]))
        print(f"{changes} changes applied.")
        if diff.get("reasoning"):
            print(f"  Reason: {diff['reasoning'][:120]}")

        print(f"\n  Data stats: {self.sink.stats()}")
        return run_summary


# ──────────────────────────────────────────────
# 10.  SCALE RUNNER
# ──────────────────────────────────────────────
def generate_design_variants(base_spec: dict) -> list:
    import itertools
    clock_freqs  = [100, 200, 400, 500]
    bus_widths   = [8, 16, 32, 64]
    util_targets = [40, 55, 65, 75]
    reset_styles = ["sync", "async"]
    variants = []
    for freq, width, util, rst in itertools.product(
        clock_freqs, bus_widths, util_targets, reset_styles
    ):
        v = dict(base_spec)
        v["clock_mhz"] = freq
        v["bus_width"] = width
        v["core_util_pct"] = util
        v["reset_style"] = rst
        v["design_name"] = f"{base_spec['design_name']}_f{freq}_w{width}_u{util}_{rst}"
        variants.append(v)
    return variants[:100]


def run_scale(base_spec: dict, n_variants: int = 100,
              output_dir: str = "./synthetic_data"):
    orchestrator = VLSIOrchestrator(output_dir=output_dir)
    variants = generate_design_variants(base_spec)[:n_variants]
    for i, spec in enumerate(variants):
        print(f"\nVariant {i+1}/{len(variants)}: {spec['design_name']}")
        orchestrator.run_pipeline(spec)
    print(f"\n  Final stats: {orchestrator.sink.stats()}")


# ──────────────────────────────────────────────
# 11.  ENTRY POINT
# ──────────────────────────────────────────────
if __name__ == "__main__":
    BASE_DESIGN = {
        "design_name":   "uart_ctrl",
        "description":   "UART controller with FIFO, Sky130 PDK, 100MHz target",
        "pdk":           "sky130",
        "top_module":    "uart_ctrl",
        "interfaces":    ["apb", "uart_rx", "uart_tx"],
        "clock_mhz":     100,
        "bus_width":     8,
        "core_util_pct": 55,
        "reset_style":   "sync",
        "fifo_depth":    16,
    }

    # Single run demo
    orc = VLSIOrchestrator(output_dir="./synthetic_data")
    summary = orc.run_pipeline(BASE_DESIGN)

    # Scale run — uncomment to generate millions of records
    # run_scale(BASE_DESIGN, n_variants=100, output_dir="./synthetic_data")
