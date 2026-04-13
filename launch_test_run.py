#!/usr/bin/env python3
"""
Quick test launch — Phase 1a, max 3 generations.
Usage: python launch_test_run.py [output_dir]
"""
import os, sys, logging, subprocess, time, signal, atexit
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "evaluation"))
sys.path.insert(0, str(ROOT.parent / "downloads/Hyperagents"))

output_dir = sys.argv[1] if len(sys.argv) > 1 else str(ROOT / "outputs/run_001")
skill_dir  = str(ROOT / "mem_gen_offline_tester")

os.makedirs(output_dir, exist_ok=True)

logging.basicConfig(
    level   = logging.INFO,
    format  = "%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt = "%H:%M:%S",
    handlers = [
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(os.path.join(output_dir, "phase1a.log")),
    ],
)

log = logging.getLogger("launch")

# ── Start Copilot proxy (handles session token exchange) ──────────────────
_proxy_proc = None

def _start_copilot_proxy():
    global _proxy_proc
    import urllib.request, json
    # Check if already running
    try:
        with urllib.request.urlopen("http://127.0.0.1:18976/health", timeout=2):
            log.info("Copilot proxy already running on :18976")
            return
    except Exception:
        pass

    proxy_script = str(ROOT / "evaluation/utils/copilot_proxy.py")
    if not os.path.exists(proxy_script):
        log.warning("copilot_proxy.py not found, skipping proxy start")
        return

    log.info("Starting Copilot proxy...")
    _proxy_proc = subprocess.Popen(
        [sys.executable, proxy_script],
        stdout=open(os.path.join(output_dir, "copilot_proxy.log"), "w"),
        stderr=subprocess.STDOUT,
    )
    # Wait for it to be ready
    for _ in range(10):
        time.sleep(1)
        try:
            with urllib.request.urlopen("http://127.0.0.1:18976/health", timeout=2):
                log.info("Copilot proxy started (pid=%d)", _proxy_proc.pid)
                return
        except Exception:
            pass
    log.error("Copilot proxy failed to start!")

def _stop_copilot_proxy():
    global _proxy_proc
    if _proxy_proc and _proxy_proc.poll() is None:
        log.info("Stopping Copilot proxy (pid=%d)", _proxy_proc.pid)
        _proxy_proc.terminate()
        try:
            _proxy_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _proxy_proc.kill()

if os.environ.get("GITHUB_TOKEN"):
    _start_copilot_proxy()
    atexit.register(_stop_copilot_proxy)
else:
    log.info("No GITHUB_TOKEN, skipping Copilot proxy")

# Override max_generations for this run
# Must match loop.py's import path (phase1a.config, not evaluation.phase1a.config)
import phase1a.config as _cfg
_cfg.PHASE1A_CONFIG["max_generations"] = 10
log.info(f"Override: max_generations=10")

log.info(f"output_dir : {output_dir}")
log.info(f"skill_dir  : {skill_dir}")

from evaluation.phase1a.loop import run_phase1a

# NOTE: MC path is no longer pre-resolved here.
# Task Agent discovers the MC binary itself via prompt instructions
# (tries: env MC_PATH, PATH, `module load mc2_n12`, /data/eda/tsmc/... search).
# If MC is not found, Task Agent aborts and does NOT write DONE.txt.

result = run_phase1a(output_dir=output_dir, skill_dir=skill_dir)

log.info(f"\n{'='*60}")
log.info(f"DONE. best_score={result['score']:.4f}")
log.info(f"best_skill_dir={result['skill_dir']}")
