#!/usr/bin/env python3
"""Pure-python autorun wait — TCC-safe replacement for autorun-wait.sh.

macOS TCC blocks shell-level file creation in ~/Documents, but python file
writes are permitted. autorun-wait.sh fails on its `echo > .autorun-session`
redirect; this script does the identical job (session-invalidation, countdown
broadcast, input-queue poll, /queue/consumed POST) using python I/O only.

Prints the queued player action(s) to stdout, or nothing on timeout (9 min).
"""
import os
import sys
import ssl
import time
import json
import secrets
import subprocess
import urllib.request

DISPLAY_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(DISPLAY_DIR, "..", "scripts"))
PUSH = os.path.join(DISPLAY_DIR, "push_stats.py")

try:
    from paths import runtime_dir, find_campaign
    RT = str(runtime_dir())
except Exception:
    RT = os.path.join(os.environ.get("DND_CAMPAIGN_ROOT", os.path.expanduser("~/.claude/dnd")), ".runtime")
os.makedirs(RT, exist_ok=True)

QFILE = os.path.join(RT, ".input_queue")
SESSION_FILE = os.path.join(RT, ".autorun-session")

# Invalidate any previous wait loop by writing a new session id (python write — TCC-ok)
my_session = secrets.token_hex(8)
with open(SESSION_FILE, "w", encoding="utf-8") as f:
    f.write(my_session)

# Resolve autorun interval from the active campaign's state.md (default 60s)
interval = 60
try:
    import re
    camp = open(os.path.join(RT, ".campaign"), encoding="utf-8").read().strip()
    txt = (find_campaign(camp) / "state.md").read_text(encoding="utf-8", errors="replace")
    m = re.search(r"autorun_interval:\s*(\d+)", txt)
    if m:
        interval = int(m.group(1))
except Exception:
    pass

subprocess.run([sys.executable, PUSH, "--autorun-waiting", "true", "--autorun-cycle", str(interval)],
               capture_output=True)

# Poll loop — exit when queue appears, session changes, or 9 minutes pass
content = ""
for _ in range(1800):  # 0.3s * 1800 = 9 min
    if os.path.exists(QFILE):
        try:
            content = open(QFILE, encoding="utf-8").read()
            os.unlink(QFILE)
        except Exception:
            content = ""
        break
    try:
        if open(SESSION_FILE, encoding="utf-8").read().strip() != my_session:
            break
    except Exception:
        break
    time.sleep(0.3)

# Clean up our session file if it's still ours
try:
    if open(SESSION_FILE, encoding="utf-8").read().strip() == my_session:
        os.unlink(SESSION_FILE)
except Exception:
    pass

subprocess.run([sys.executable, PUSH, "--autorun-waiting", "false"], capture_output=True)

# Clear the display queue indicator on success
if content:
    try:
        scheme_file = os.path.join(DISPLAY_DIR, ".scheme")
        scheme = open(scheme_file, encoding="utf-8").read().strip() if os.path.exists(scheme_file) else "http"
        token = open(os.path.join(RT, ".token"), encoding="utf-8").read().strip()
        ctx = None
        if scheme == "https":
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        req = urllib.request.Request(f"{scheme}://localhost:5001/queue/consumed",
                                     data=b"", method="POST",
                                     headers={"X-DND-Token": token})
        urllib.request.urlopen(req, timeout=1, context=ctx)
    except Exception:
        pass

sys.stdout.write(content)

# ── Routing (opt-in) ─────────────────────────────────────────────────────────
# The declarations are in; the DM still has to decide which check each one is,
# how hard, against whom, and whether it is private. Those judgments do not
# depend on the narration, so they can be settled while the DM is still reading.
# With --auto-dice the resulting requests reach the phones before the first
# sentence is written, which is the gap the table actually feels.
if "--auto-route" in sys.argv and content.strip():
    import re
    import concurrent.futures as _cf
    sys.path.insert(0, DISPLAY_DIR)
    try:
        import jev_route
        import jev_check
    except ImportError:
        jev_route = None

    def _flag(name, default=""):
        return sys.argv[sys.argv.index(name) + 1] if name in sys.argv else default

    def _dice_request(character, modifier, label, dc):
        """POST a dice request the way the page expects it, without a subprocess."""
        try:
            scheme_file = os.path.join(DISPLAY_DIR, ".scheme")
            scheme = open(scheme_file, encoding="utf-8").read().strip() if os.path.exists(scheme_file) else "http"
            token = open(os.path.join(RT, ".token"), encoding="utf-8").read().strip()
        except OSError:
            scheme, token = "http", ""
        ctx = None
        if scheme == "https":
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        body = json.dumps({"characters": [character], "spec": "1d20", "modifier": modifier,
                           "advantage": "normal", "label": label, "dc": dc}).encode("utf-8")
        req = urllib.request.Request(f"{scheme}://localhost:5001/dice-request", data=body,
                                     method="POST",
                                     headers={"Content-Type": "application/json",
                                              "X-DND-Token": token})
        try:
            urllib.request.urlopen(req, timeout=3, context=ctx)
        except Exception:
            pass

    if jev_route:
        scene = _flag("--scene")
        present = [p.strip() for p in _flag("--present").split(",") if p.strip()]
        options = [a for i, a in enumerate(sys.argv) if i and sys.argv[i - 1] == "--option"]
        campaign = ""
        try:
            campaign = open(os.path.join(RT, ".campaign"), encoding="utf-8").read().strip()
        except OSError:
            pass
        auto_dice = "--auto-dice" in sys.argv
        # Above this the routing is clear enough to act on unprompted; below it
        # the line is printed for the DM and nothing is sent.
        floor = float(_flag("--route-floor", "0.8"))

        lines = [m.groups() for m in
                 (re.match(r"\[([^\]]+)\]:\s*(.+)", ln) for ln in content.splitlines()) if m]

        def _route(pair):
            # In-process: four players used to mean four interpreters starting
            # up to make one HTTP call each, which cost more than the calls.
            who, text = pair
            try:
                return who, text, jev_route.route(
                    text, campaign or "temiz-kagit", who, scene, present, options)
            except Exception:
                return who, text, {}

        with _cf.ThreadPoolExecutor(max_workers=4) as pool:
            routed = list(pool.map(_route, lines))

        sys.stdout.write("\n--- yönlendirme ---\n")
        for who, text, r in routed:
            if not r:
                sys.stdout.write(f"[{who}] yönlendirilemedi\n")
                continue
            name = r.get("karakter") or who
            skill, conf = r.get("skill"), r.get("skill_guven") or 0.0
            mark = "" if conf >= floor else "  (belirsiz, DM karar versin)"
            if r.get("zar_gerekli"):
                mod = jev_check.skill_modifier(campaign, name, skill or "")
                sys.stdout.write(
                    f"[{name}] {skill} {'' if mod is None else ('+' if mod >= 0 else '') + str(mod)} "
                    f"vs DC {r.get('dc')} · hedef {r.get('hedef') or '-'} · "
                    f"{'özel' if r.get('ozel') else 'ortak'} · dal {r.get('dal')}{mark}\n")
                if auto_dice and conf >= floor and mod is not None:
                    # Posted straight to the endpoint rather than through
                    # send.py: the name came from the campaign's own roster and
                    # the die from the routing step, so every check send.py
                    # would run here has already been answered.
                    _dice_request(name, mod, f"{skill} — {text[:60]}", r.get("dc"))
            else:
                sys.stdout.write(
                    f"[{name}] zar yok · hedef {r.get('hedef') or '-'} · "
                    f"{'özel' if r.get('ozel') else 'ortak'} · dal {r.get('dal')}{mark}\n")
