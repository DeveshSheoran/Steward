import subprocess
import sys


def test_cli_help_and_empty_commands(tmp_path):
    env = {"STEWARD_DATA_DIR": str(tmp_path / "d"), "PATH": "/usr/bin:/bin"}
    run = lambda *a: subprocess.run([sys.executable, "-m", "steward", *a], capture_output=True, text=True, env={**env, "PYTHONPATH": "."}, timeout=60)
    assert run("--help").returncode == 0
    out = run("verify-audit")
    assert "audit chain: OK" in out.stdout, out.stderr
    tid = run("submit", "do a thing").stdout.strip()
    assert tid.startswith("task_")
    assert tid in run("list").stdout
