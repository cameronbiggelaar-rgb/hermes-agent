"""EXT-RESTORE-002-02 — integrity audit of the release tree (read-only, no repo mutation)."""

import ast
import hashlib
import json
import os
import subprocess
import sys

W = "/home/hermes/work/restore002-fix"
BASE = "5d02e1da"          # PR #9 head the owner reviewed
DEPLOYED = "3652a370"      # verified live install (EXT-17 baseline)


def git(*args):
    return subprocess.run(["git", *args], cwd=W, capture_output=True, text=True, check=False).stdout.strip()


changed = [f for f in git("diff", "--name-only", BASE).splitlines() if f]
untracked = [f for f in git("ls-files", "--others", "--exclude-standard").splitlines() if f]
HEAD = git("rev-parse", "HEAD")
TREE = git("rev-parse", "HEAD^{tree}")

targets = sorted(set(changed + untracked + [
    "gateway/session_resume_plan.py", "harness/fault_b_repro.py",
    "gateway/session_recovery.py", "tests/gateway/test_session_resume_plan.py",
    "tests/gateway/test_stop_budget_adaptation.py",
    "docs/restore002/RESTORE-002-EXTERNAL-CA-PACKAGE.md",
]))
targets = [t for t in targets if os.path.exists(os.path.join(W, t))]

problems = []
report = []
for f in targets:
    p = os.path.join(W, f)
    raw = open(p, "rb").read()
    try:
        src = raw.decode("utf-8")
    except UnicodeDecodeError as e:
        problems.append((f, f"not utf-8: {e}"))
        continue
    lines = src.splitlines()
    if not raw.endswith(b"\n"):
        problems.append((f, "no trailing newline"))
    if b"\x00" in raw:
        problems.append((f, "NUL byte"))
    if "\ufffd" in src:
        problems.append((f, "U+FFFD replacement character"))
    if not f.endswith(".py"):
        report.append((f, len(lines), "-", hashlib.sha256(raw).hexdigest()[:12]))
        continue
    try:
        tree = ast.parse(src)
    except SyntaxError as e:
        problems.append((f, f"SyntaxError {e}"))
        continue
    # A patch that eats a closing triple-quote leaves the docstring as a live expression that
    # still parses; the tell is a module/class/function whose first statement is not a Constant.
    doc_nodes = 0
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            first = body[0] if body else None
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str):
                doc_nodes += 1
    quotes = src.count('"""') + src.count("'''")
    if quotes % 2:
        problems.append((f, f"odd triple-quote count ({quotes})"))
    report.append((f, len(lines), doc_nodes, hashlib.sha256(raw).hexdigest()[:12]))

# Byte identity of the file whose edit was reported as failed.
srec_work = open(os.path.join(W, "gateway/session_recovery.py"), "rb").read()
srec_base = subprocess.run(["git", "show", f"{DEPLOYED}:gateway/session_recovery.py"],
                           cwd=W, capture_output=True).stdout
srec_head = subprocess.run(["git", "show", f"{BASE}:gateway/session_recovery.py"],
                           cwd=W, capture_output=True).stdout

# Any half-written index/objects from an aborted edit?
fsck = git("fsck", "--dangling", "--no-progress")
dangling = [ln for ln in fsck.splitlines() if "dangling" in ln]

# Do the new modules import cleanly in a fresh interpreter?
imp = subprocess.run(
    [sys.executable, "-c",
     "import gateway.session_resume_plan as m, harness.fault_b_repro as h;"
     "print('resume_plan ok', len([n for n in dir(m) if not n.startswith('_')]));"
     "print('fault_b_repro ok', len([n for n in dir(h) if not n.startswith('_')]))"],
    cwd=W, capture_output=True, text=True,
    env={**os.environ, "PYTHONPATH": W},
)

print(json.dumps({
    "head": HEAD, "tree": TREE, "base": BASE, "deployed": DEPLOYED,
    "changed_vs_base": changed, "untracked": untracked,
    "files_audited": len(targets),
    "problems": problems,
    "session_recovery": {
        "worktree_sha256": hashlib.sha256(srec_work).hexdigest(),
        "base_sha256": hashlib.sha256(srec_base).hexdigest(),
        "head_sha256": hashlib.sha256(srec_head).hexdigest(),
        "identical_to_base": srec_base == srec_work,
        "bytes": len(srec_work),
    },
    "dangling_objects": dangling[:5],
    "imports": (imp.stdout + imp.stderr).strip().splitlines(),
}, indent=2))
print()
print(f"{'file':60s} {'lines':>5s} {'docs':>4s} sha256[:12]")
for f, n, d, h in report:
    print(f"{f:60s} {n:5d} {str(d):>4s} {h}")
