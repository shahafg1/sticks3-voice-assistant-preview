"""Run every automated suite under the isolation guard, optionally inside a throw-away copy of the project.

    python tests/run_all.py                run all suites here (the guard blocks production paths and live endpoints)
    python tests/run_all.py --sandbox      first copy the project (tracked and untracked files, no data/secrets/recordings,
                                           no backups) to a temporary directory with its own HOME and TMPDIR, run the suites
                                           there, and fail if anything created data/, secrets/, recordings/ or ~/.claude
                                           in the copy or logged a guard violation
    python tests/run_all.py test_reminders test_sleep      only these suites

Only tests/test_*.py are run. Suites named test_history_*.py check upgrades and rollbacks against this project's own development
history (its git tags) and the files that only the developers' repository has; they are not part of a published package, where
every remaining suite must pass. A copy without git (an extracted release archive) is sandboxed by copying the files.
Hardware and manual checks (a real Stick, the live server, the desktop launcher) are NOT part of
this suite: see tests/README.md. Interpreter: STICK_TEST_PYTHON, else voice-env/bin/python if present, else this one.
"""
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
SKIP = {"data", "secrets", "recordings", "voice-env", "tts-env312", "tts-models", "tools-env", "whisper-model", ".git", ".playwright-mcp",
        "__pycache__", ".stick-setup"}
HAS_GIT = (ROOT / ".git").exists()


def interpreter():
    configured = os.environ.get("STICK_TEST_PYTHON")
    if configured:
        return configured
    voice = ROOT / "voice-env" / "bin" / "python"
    return str(voice) if voice.exists() else sys.executable


def project_files():
    """Tracked and untracked-but-not-ignored files, as `git ls-files` lists them (read-only); without git, every file outside SKIP."""
    if not HAS_GIT:
        private = {"config/host.conf"}                    # this Mac's own configuration never goes into the sandbox
        return [rel for rel in (p.relative_to(ROOT).as_posix() for p in ROOT.rglob("*") if p.is_file())
                if not (set(rel.split("/")) & SKIP) and rel not in private and not rel.endswith(".pyc")]
    out = subprocess.run(["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"], cwd=ROOT,
                         capture_output=True, check=True).stdout.decode().split("\0")
    return [f for f in out if f and not (set(Path(f).parts) & SKIP) and "backup" not in Path(f).parts[0]]


def make_sandbox():
    box = Path(tempfile.mkdtemp(prefix="stick-sandbox-"))
    project = box / "project"
    if HAS_GIT:
        subprocess.run(["git", "clone", "--quiet", "--no-hardlinks", str(ROOT), str(project)], check=True)   # tags for `git show`
    else:
        project.mkdir()
    for name in project_files():
        source = ROOT / name
        if source.is_file():
            target = project / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
    (box / "home").mkdir()
    (box / "tmp").mkdir()
    return box, project


def main(argv):
    sandbox = "--sandbox" in argv
    names = [a for a in argv if not a.startswith("--")]
    box = None
    root = ROOT
    env = dict(os.environ)
    for key in [k for k in env if k.startswith("STICK_") and k != "STICK_TEST_PYTHON"]:
        env.pop(key)                                            # a stray STICK_DATA_DIR from a shell must not leak in
    log_dir = Path(tempfile.mkdtemp(prefix="stick-guard-"))
    if sandbox:
        box, root = make_sandbox()
        env.update({"HOME": str(box / "home"), "TMPDIR": str(box / "tmp")})
        log_dir = box
        print(f"sandbox: {root}")
    guard_log = log_dir / "guard.log"
    env["STICK_TEST_GUARD_LOG"] = str(guard_log)
    suites = sorted((root / "tests").glob("test_*.py"))
    if names:
        suites = [s for s in suites if s.stem in names]
    python = interpreter()
    failed = []
    for suite in suites:
        result = subprocess.run([python, str(suite)], cwd=root, env=env, capture_output=True, text=True, timeout=900)
        last = (result.stdout.strip().splitlines() or ["(no output)"])[-1]
        print(f"{'ok  ' if result.returncode == 0 else 'FAIL'} {suite.stem:28s} {last}")
        if result.returncode != 0:
            failed.append(suite.stem)
            print("\n".join(result.stdout.splitlines()[-8:]), result.stderr[-800:], sep="\n")
    problems = []
    if guard_log.exists() and guard_log.read_text().strip():
        problems.append("guard violations logged:\n" + guard_log.read_text())
    if sandbox:
        for name in ("data", "secrets", "recordings"):
            if (root / name).exists():
                problems.append(f"the suites created {name}/ in the project copy")
        if any((box / "home").iterdir()):
            problems.append("the suites wrote into HOME: " + ", ".join(p.name for p in (box / "home").iterdir()))
        shutil.rmtree(box, ignore_errors=True)
    for problem in problems:
        print("PROBLEM:", problem)
    print(f"{len(suites) - len(failed)}/{len(suites)} suites passed" + (" (sandboxed)" if sandbox else ""))
    return 1 if failed or problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
