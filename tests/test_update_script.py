"""deploy/update.sh --rebuild against a stand-in docker that prints its
build contexts the way Compose does: services sorted by name, so
livesync-bridge's context comes before the clone's own."""
import os
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "deploy" / "update.sh"

FAKE_DOCKER = """#!/bin/sh
if [ "$1 $2" = "compose config" ] && [ "$3" != "-q" ]; then
  for c in $CONTEXTS; do echo "      context: $c"; done
  exit 0
fi
echo "docker $*" >> "$CALLS"
"""


@pytest.fixture
def clone(tmp_path):
    if not shutil.which("git"):
        pytest.skip("git not installed")
    repo = tmp_path / "clone"
    (repo / "deploy").mkdir(parents=True)
    shutil.copy(SCRIPT, repo / "deploy" / "update.sh")
    git = ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.com"]
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(git + ["add", "-A"], check=True)
    subprocess.run(git + ["commit", "-q", "-m", "clone"], check=True)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "docker").write_text(FAKE_DOCKER)
    (bin_dir / "docker").chmod(0o755)
    return repo, bin_dir, tmp_path / "calls"


def run(clone, contexts):
    repo, bin_dir, calls = clone
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "CALLS": str(calls),
           "CONTEXTS": " ".join(contexts)}
    return subprocess.run(["bash", str(repo / "deploy" / "update.sh"), "--rebuild", "-y"],
                          cwd=repo, env=env, capture_output=True, text=True)


def test_builds_when_the_clone_is_one_of_the_contexts(clone):
    repo = clone[0]
    result = run(clone, [f"{repo}/livesync", str(repo)])
    assert result.returncode == 0, result.stderr
    assert "docker compose up -d" in clone[2].read_text()


def test_refuses_when_stack_dir_points_elsewhere(clone):
    result = run(clone, ["/elsewhere/livesync", "/elsewhere"])
    assert result.returncode == 1
    assert "STACK_DIR" in result.stderr
    calls = clone[2].read_text() if clone[2].exists() else ""
    assert "build" not in calls and "up -d" not in calls
