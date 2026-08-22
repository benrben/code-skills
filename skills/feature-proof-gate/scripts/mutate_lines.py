#!/usr/bin/env python3
"""Hardener v2 mutation runner — FULL operator set (no G/H split), plus
if-condition forcing and return-value nulling. Mutates only lines changed vs
base_ref in files matching src_glob; runs the suite per mutant.

Usage: mutate_lines2.py --root DIR --base REF --glob PATTERN
                        --test-cmd "python3 -m pytest -q" [--max-mutants N]
                        [--list-only]
JSON out: {"mutants": n, "killed": n, "survived": n, "survivors": [...]}
--list-only: no test runs; {"mutants": n} (density pre-check).
"""
import argparse, fnmatch, json, pathlib, re, subprocess, sys

OPS = [("==", "!="), ("!=", "=="), ("<=", ">"), (">=", "<"), ("<", ">="), (">", "<="),
       (" + ", " - "), (" - ", " + "), (" and ", " or "), (" or ", " and "),
       ("True", "False"), ("False", "True"),
       (" not in ", " in "), (" in ", " not in "),
       (" is not ", " is "), (" is ", " is not "),
       ("min(", "max("), ("max(", "min(")]

def run(cmd, cwd, timeout=300):
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)

def quote_safe(line):
    segs, i, out, q, start = [], 0, True, "", 0
    while i < len(line):
        ch = line[i]
        if out and ch in "\"'":
            segs.append((start, i)); out, q = False, ch
        elif not out and ch == q and (i == 0 or line[i - 1] != "\\"):
            out, start = True, i + 1
        i += 1
    if out:
        segs.append((start, len(line)))
    return segs

def changed_line_numbers(root, base, relpath):
    d = run(["git", "diff", "-U0", base, "--", relpath], root).stdout
    nums = set()
    for m in re.finditer(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", d, re.M):
        start, n = int(m.group(1)), int(m.group(2) or 1)
        nums.update(range(start, start + n))
    return nums

def target_files(root, base, glob):
    tracked = run(["git", "diff", "--name-only", base], root).stdout.splitlines()
    untracked = [l.split(None, 1)[1] for l in
                 run(["git", "status", "--porcelain", "--untracked-files=all"], root).stdout.splitlines()
                 if l.startswith("??")]
    files = {}
    for f in tracked:
        if fnmatch.fnmatch(f, glob) and (pathlib.Path(root) / f).exists():
            files[f] = changed_line_numbers(root, base, f)
    for f in untracked:
        if fnmatch.fnmatch(f, glob):
            p = pathlib.Path(root) / f
            if p.exists():
                files[f] = set(range(1, len(p.read_text().splitlines()) + 1))
    return files

def gen_mutants(line):
    muts = []
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return muts
    for old, new in OPS:
        for s, e in quote_safe(line):
            idx = line.find(old, s, e)
            while idx != -1:
                muts.append((line[:idx] + new + line[idx + len(old):], old.strip() or old))
                idx = line.find(old, idx + 1, e)
    for s, e in quote_safe(line):
        for m in re.finditer(r"(?<![\w.])(\d+)(?![\w.])", line[s:e]):
            i0, i1 = s + m.start(1), s + m.end(1)
            muts.append((line[:i0] + str(int(m.group(1)) + 1) + line[i1:], f"int{m.group(1)}+1"))
    m = re.match(r"^(\s*)(el)?if (.+):(\s*(#.*)?)$", line.rstrip("\n"))
    if m and "True:" not in line and "False:" not in line:
        kw = (m.group(2) or "") + "if"
        for forced in ("True", "False"):
            muts.append((f"{m.group(1)}{kw} {forced}:  # forced\n"
                         if not m.group(2) else
                         f"{m.group(1)}elif {forced}:  # forced\n", f"if->{forced}"))
    m = re.match(r"^(\s*)return (.+?)(\s*(#.*)?)$", line.rstrip("\n"))
    if m and m.group(2).strip() != "None":
        muts.append((f"{m.group(1)}return None\n", "return->None"))
    return muts

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--base", required=True)
    ap.add_argument("--glob", required=True)
    ap.add_argument("--test-cmd", default="python3 -m pytest -q")
    ap.add_argument("--max-mutants", type=int, default=250)
    ap.add_argument("--list-only", action="store_true")
    a = ap.parse_args()
    root = pathlib.Path(a.root)
    tcmd = a.test_cmd.split()
    killed = survived = total = 0
    survivors = []
    for rel, nums in target_files(root, a.base, a.glob).items():
        p = root / rel
        orig = p.read_text()
        src = orig.splitlines(keepends=True)
        try:
            for ln in sorted(nums):
                if ln - 1 >= len(src):
                    continue
                for mline, op in gen_mutants(src[ln - 1]):
                    if total >= a.max_mutants:
                        break
                    total += 1
                    if a.list_only:
                        continue
                    p.write_text("".join(src[:ln - 1]) + mline + "".join(src[ln:]))
                    r = run(tcmd + ["-x", "-p", "no:cacheprovider"], root, timeout=180)
                    if r.returncode == 0:
                        survived += 1
                        survivors.append(f"{rel}:{ln}:{op}")
                    else:
                        killed += 1
        finally:
            p.write_text(orig)
    if a.list_only:
        print(json.dumps({"mutants": total}))
    else:
        print(json.dumps({"mutants": total, "killed": killed, "survived": survived,
                          "survivors": survivors}))

if __name__ == "__main__":
    main()
