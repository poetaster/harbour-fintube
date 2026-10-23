#!/usr/bin/env python3
"""Which engine code do the tests actually run? Pure standard library (no coverage.py needed).

    python3 tests/coverage_report.py            # report: % of lines run per module, and every
                                                #   function no test runs
    python3 tests/coverage_report.py --check    # CI: fail if a function that ISN'T in the baseline
                                                #   is untested (new code arrived without a test)
    python3 tests/coverage_report.py --update   # rewrite the baseline after adding tests

The baseline (tests/coverage_baseline.txt) lists the functions no offline test can run, each
with the reason. It may only shrink: test one and --update drops it; a new untested function fails --check. Runs the
engine suite (python/test_youfish.py) under a line tracer, threads included (the media proxy and
stream readers run on their own threads), plus every tests/test_*.py (the parsers against
recorded YouTube answers).
"""
import ast
import io
import os
import sys
import threading
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PYDIR = os.path.join(ROOT, "python")
BASELINE = os.path.join(HERE, "coverage_baseline.txt")


def _modules():
    return sorted(f for f in os.listdir(PYDIR)
                  if f.endswith(".py") and not f.startswith("test_"))


def _functions(path):
    """{qualname: (def line, {lines that identify it})} for module-level functions and class
    methods. A code object's first line is the def line, or the first decorator's."""
    tree = ast.parse(open(path).read(), path)
    out = {}

    def add(node, prefix):
        out[prefix + node.name] = (node.lineno, {node.lineno} | {d.lineno for d in node.decorator_list})

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            add(node, "")
        elif isinstance(node, ast.ClassDef):
            for sub in node.body:
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    add(sub, node.name + ".")
    return out


def _statement_lines(path):
    tree = ast.parse(open(path).read(), path)
    return {n.lineno for n in ast.walk(tree)
            if isinstance(n, ast.stmt) and not isinstance(n, (ast.FunctionDef, ast.ClassDef,
                                                             ast.AsyncFunctionDef))}


def run_traced():
    """Run every Python test module; return ({file: set(first lines of called code)}, {file: set(lines)},
    unittest result)."""
    targets = {os.path.join(PYDIR, m) for m in _modules()}
    called, lines = {t: set() for t in targets}, {t: set() for t in targets}

    def local(frame, event, arg):
        if event == "line":
            lines[frame.f_code.co_filename].add(frame.f_lineno)
        return local

    def tracer(frame, event, arg):
        fn = frame.f_code.co_filename
        if fn not in called:
            return None
        if event == "call":
            called[fn].add(frame.f_code.co_firstlineno)
            lines[fn].add(frame.f_lineno)
        return local

    sys.path.insert(0, PYDIR)
    os.chdir(PYDIR)
    threading.settrace(tracer)
    sys.settrace(tracer)
    try:
        sys.path.insert(0, HERE)
        import importlib
        names = [f[:-3] for d in (PYDIR, HERE) for f in sorted(os.listdir(d))
                 if f.startswith("test_") and f.endswith(".py")]
        suite = unittest.TestSuite(
            unittest.defaultTestLoader.loadTestsFromModule(importlib.import_module(n))
            for n in names)
        result = unittest.TextTestRunner(stream=io.StringIO(), verbosity=0).run(suite)
    finally:
        sys.settrace(None)
        threading.settrace(None)
    return called, lines, result


def untested():
    called, lines, result = run_traced()
    report, missing = [], []
    for m in _modules():
        path = os.path.join(PYDIR, m)
        funcs = _functions(path)
        stmts = _statement_lines(path)
        ran = len(stmts & lines[path])
        never = sorted((ln, name) for name, (ln, ident) in funcs.items()
                       if not (ident & called[path]))
        report.append((m, ran, len(stmts), len(funcs), len(never)))
        missing += ["%s:%s" % (m[:-3], name) for _, name in never]
    return report, missing, result


def _load_baseline():
    """{module:function: reason} from the baseline; each line is `name  # why it can't run offline`."""
    out = {}
    try:
        with open(BASELINE) as f:
            for line in f:
                name, _, why = line.partition("#")
                if name.strip():
                    out[name.strip()] = why.strip()
    except OSError:
        pass
    return out


def main(argv):
    report, missing, result = untested()
    if not result.wasSuccessful():
        print("NOTE: %d engine test(s) failed under the tracer; run python/test_youfish.py"
              % (len(result.failures) + len(result.errors)))
    print("%-14s %16s %22s" % ("module", "lines run", "functions untested"))
    for m, ran, total, nf, nn in report:
        print("%-14s %6d/%-5d %3d%% %10d/%-5d" % (m, ran, total, 100 * ran // max(total, 1), nn, nf))

    base = _load_baseline()
    if "--update" in argv:
        width = max([len(m) for m in missing] + [0]) + 2
        with open(BASELINE, "w") as f:
            f.write("# Engine functions no offline test runs, each with the reason "
                    "(tests/coverage_report.py --update).\n"
                    "# This list may only shrink: test one, then --update. New code needs a test.\n")
            for m in sorted(missing):
                f.write("%s# %s\n" % (m.ljust(width), base.get(m) or "TODO: why can't this be tested?"))
        print("baseline written: %d untested functions" % len(missing))
        return 0

    new = sorted(set(missing) - set(base))
    fixed = sorted(set(base) - set(missing))
    if "--check" in argv:
        if fixed:
            print("Now tested, drop from the baseline (--update): " + ", ".join(fixed))
        if new:
            print("UNTESTED NEW CODE — add a test (or, if it truly can't run offline, --update "
                  "and say why in the commit):")
            for s in new:
                print("  " + s)
            return 1
        print("coverage check OK: no new untested functions")
        return 0

    print("\nFunctions no test runs (%d):" % len(missing))
    for s in missing:
        print("  " + s + ("" if s in base else "   <- NEW"))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
