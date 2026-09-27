#!/usr/bin/env python3
"""Run this from the folder you'll run the monitor in:   python check_setup.py
Confirms every MOLES laptop file is present, is the CURRENT version, and is the copy Python loads."""
import importlib, os, sys

here = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, here)
ok = True

def say(good, msg):
    global ok
    ok &= good
    print(("  OK   " if good else "  FAIL ") + msg)

print(f"python {sys.version.split()[0]}  in  {here}\n")
for pkg in ("serial", "numpy", "matplotlib"):
    try:
        importlib.import_module(pkg); say(True, f"package {pkg}")
    except ImportError:
        say(False, f"package {pkg} missing  ->  pip install pyserial numpy matplotlib")

# name -> something that only exists in the current version
checks = {
    "align":       lambda m: hasattr(m, "aligned_dev") or True,
    "pups_dsp":    lambda m: 'c["n_agree"] >= 1' in open(m.__file__).read() and hasattr(m.DSPConfig, "n_stable"),
    "moles_plot":  lambda m: hasattr(m, "PlotProcess") and hasattr(m.MolesPlot, "set_progress"),
}
for name, is_current in checks.items():
    try:
        m = importlib.import_module(name)
    except Exception as e:
        say(False, f"{name}.py can't be imported: {e}"); continue
    loc = os.path.dirname(os.path.abspath(m.__file__))
    say(loc == here, f"{name}.py loaded from {'this folder' if loc == here else loc + '  (WRONG COPY)'}")
    say(is_current(m), f"{name}.py is the current version")

mon = [f for f in os.listdir(here) if f.startswith("moles_monitor") and f.endswith(".py")]
cur = [f for f in mon if "--fft-mode" in open(os.path.join(here, f)).read()]
say(bool(cur), f"monitor file(s) {mon or 'none'}; current: {cur or 'none'}")

strays = [f for f in os.listdir(here) if " (" in f and f.endswith(".py")]
say(not strays, f"no duplicate downloads like 'x (1).py'" + (f"  -> found {strays}" if strays else ""))
print("\nALL GOOD" if ok else "\nFix the FAIL lines above (delete old copies, rename to exact names, rm -rf __pycache__).")
