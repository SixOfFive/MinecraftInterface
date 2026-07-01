"""controller.py — the entrypoint.

Spawns one or more Node Mineflayer bots (bot.js), connects to each control
bridge, wires Minecraft chat into the LLM agent, and gives you a small console
to set goals. Console commands apply to ALL bots at once.

Run:  python controller.py --goal "follow me and say hi"
      python controller.py --bot 3 --mc-port 12345      # three bots at once
Everything is configurable by flag or environment variable; see --help.

Ctrl-C stops every bot cleanly — see the process-leak safety section below.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import shutil
import signal
import socket
import sys
import threading
from pathlib import Path

from agent import Agent
from bridge import BotBridge, BotError
from ollama_client import DEFAULT_MODEL, DEFAULT_OLLAMA_URL, OllamaClient, OllamaError

BOT_DIR = Path(__file__).resolve().parent / "bot"

# Standing "jobs" — persistent directives set once that never time out.
JOB_PRESETS = {
    "guard": "Guard this spot. Stay within ~8 blocks of where you are now and attack any hostile mobs that approach. Do not wander far.",
    "patrol": "Patrol the nearby area: keep walking around and attack any hostile mobs you see.",
    "progress": "Gear up: use gearUp to craft the next tool you need; when it says you need wood or stone, harvestNearest to get it, then gearUp again. Keep upgrading until you have stone tools.",
    "play": "Play like a real Minecraft player: keep your gear upgraded (gearUp + harvestNearest), then VARY what you do — gather, explore new spots, build, hunt, stash loot in a chest. Don't stand still or repeat one thing.",
    "harvest": "Gather resources continuously: use harvestNearest to mine and collect the nearest useful block. When your inventory is getting full, use stashResources to deposit everything into a chest, then keep gathering.",
    "stash": "Deposit your loot: use stashResources to walk to the nearest chest and store all your gathered resources.",
    "lumberjack": "Chop wood continuously: mine the nearest log (oak_log, birch_log, etc.) and collect the drops. When your inventory fills up, use stashResources to store the wood, then keep going.",
    "miner": "Mine continuously: dig the nearest ore or stone and collect the drops. When your inventory fills up, use stashResources to store it. Stay safe.",
    "defend": "Follow {arg} closely and protect them: attack any hostile mobs near them.",
    "gather": "Gather {arg}: repeatedly mine the nearest {arg} and collect it. Use stashResources to store it when your inventory fills up.",
}


# ---------------------------------------------------------------------------
# Process-leak safety (Windows). Put every bot.js child in a Job Object flagged
# KILL_ON_JOB_CLOSE. The controller holds the only handle to that job, so if the
# controller exits for ANY reason — clean quit, Ctrl-C, uncaught crash, even
# taskkill — Windows closes the handle and terminates every child with it. That
# guarantees no orphaned `node` processes are left behind. All fail-soft: if the
# ctypes calls fail we simply fall back to the explicit terminate-on-shutdown.
# ---------------------------------------------------------------------------
_JOB_HANDLE = None


def _win_job_setup() -> None:
    global _JOB_HANDLE
    if os.name != "nt" or _JOB_HANDLE is not None:
        return
    try:
        import ctypes
        from ctypes import wintypes

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [("ReadOperationCount", ctypes.c_ulonglong),
                        ("WriteOperationCount", ctypes.c_ulonglong),
                        ("OtherOperationCount", ctypes.c_ulonglong),
                        ("ReadTransferCount", ctypes.c_ulonglong),
                        ("WriteTransferCount", ctypes.c_ulonglong),
                        ("OtherTransferCount", ctypes.c_ulonglong)]

        class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64),
                        ("PerJobUserTimeLimit", ctypes.c_int64),
                        ("LimitFlags", wintypes.DWORD),
                        ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t),
                        ("ActiveProcessLimit", wintypes.DWORD),
                        ("Affinity", ctypes.c_size_t),
                        ("PriorityClass", wintypes.DWORD),
                        ("SchedulingClass", wintypes.DWORD)]

        class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
                        ("IoInfo", IO_COUNTERS),
                        ("ProcessMemoryLimit", ctypes.c_size_t),
                        ("JobMemoryLimit", ctypes.c_size_t),
                        ("PeakProcessMemoryUsed", ctypes.c_size_t),
                        ("PeakJobMemoryUsed", ctypes.c_size_t)]

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateJobObjectW.restype = wintypes.HANDLE
        k32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
        job = k32.CreateJobObjectW(None, None)
        if not job:
            return
        info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
        if not k32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)):  # 9 = ExtendedLimitInformation
            k32.CloseHandle(job)
            return
        _JOB_HANDLE = job
    except Exception:
        _JOB_HANDLE = None


def _assign_to_job(pid: int) -> None:
    if os.name != "nt" or _JOB_HANDLE is None:
        return
    try:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        handle = k32.OpenProcess(0x0100 | 0x0001, False, pid)  # SET_QUOTA | TERMINATE
        if not handle:
            return
        try:
            k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
            k32.AssignProcessToJobObject(_JOB_HANDLE, handle)
        finally:
            k32.CloseHandle(handle)
    except Exception:
        pass


def install_signal_handlers(loop: asyncio.AbstractEventLoop, shutdown: asyncio.Event, holders: list) -> None:
    """First Ctrl-C / SIGTERM asks for a graceful shutdown (which terminates every
    bot.js). A second one force-exits — killing tracked children first, and the
    Job Object reaps anything else as the process dies. Prevents leaked children."""
    state = {"n": 0}

    def handler(signum, frame):  # noqa: ANN001
        state["n"] += 1
        if state["n"] == 1:
            print("\n[controller] Ctrl-C — stopping all bots (press again to force) ...", flush=True)
            try:
                loop.call_soon_threadsafe(shutdown.set)
            except RuntimeError:
                shutdown.set()
        else:
            print("\n[controller] force exit — killing children.", flush=True)
            for h in holders:
                p = h.get("p")
                if p and p.returncode is None:
                    try:
                        p.kill()
                    except Exception:
                        pass
            os._exit(1)  # Job Object closes with the process and reaps any stragglers

    for sig in (signal.SIGINT, getattr(signal, "SIGTERM", None)):
        if sig is None:
            continue
        try:
            signal.signal(sig, handler)
        except (ValueError, OSError):
            pass  # not the main thread / unsupported on this platform


def _find_free_port(host: str, start: int, limit: int = 20) -> int:
    """First bindable TCP port at/after `start` — lets each bot auto-pick a bridge port."""
    for p in range(start, start + limit):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind((host, p))
                return p
            except OSError:
                continue
    return start


def load_dotenv() -> None:
    """Load KEY=VALUE lines from a local, gitignored .env into the environment.

    Keeps machine-specific settings (your Ollama endpoint, owner name, ...) out of
    git. Real environment variables and CLI flags still take precedence.
    """
    env_path = Path(__file__).resolve().parent / ".env"
    if not env_path.exists():
        return
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


def parse_args() -> argparse.Namespace:
    load_dotenv()  # local .env fills defaults before we read them
    env = os.environ.get
    p = argparse.ArgumentParser(description="Local-LLM controller for a Minecraft (Java) character.")
    p.add_argument("--mc-host", default=env("MC_HOST", "127.0.0.1"))
    p.add_argument("--mc-port", type=int, default=int(env("MC_PORT", "25565")),
                   help="Server port. For an Open-to-LAN world, use the port printed in the game chat.")
    p.add_argument("--bot", "--bots", type=int, dest="bots", default=int(env("BOT_COUNT", "1")),
                   help="How many bots to spawn from this one process (default 1). Ctrl-C stops them all.")
    p.add_argument("--username", default=env("MC_USERNAME", ""),
                   help="Bot player name. Blank = random ClaudeBot###. With --bot N>1 it's a name prefix.")
    p.add_argument("--auth", default=env("MC_AUTH", "offline"), choices=["offline", "microsoft"])
    p.add_argument("--owner", default=env("MC_OWNER", ""), help="Player the bot protects / flees toward.")
    p.add_argument("--mc-version", default=env("MC_VERSION", ""),
                   help="Blank = auto-detect (recommended). If pinning, use a supported anchor (e.g. 1.21.8).")
    p.add_argument("--bridge-host", default=env("BRIDGE_HOST", "127.0.0.1"))
    p.add_argument("--bridge-port", type=int, default=int(env("BRIDGE_PORT", "25585")),
                   help="Base control-bridge port; bot i uses base+i (each auto-bumps if taken).")
    p.add_argument("--ollama-url", default=env("OLLAMA_URL", DEFAULT_OLLAMA_URL))
    p.add_argument("--model", default=env("OLLAMA_MODEL", DEFAULT_MODEL))
    p.add_argument("--temperature", type=float, default=float(env("OLLAMA_TEMP", "0.3")))
    p.add_argument("--tick", type=float, default=float(env("AGENT_TICK", "2.0")),
                   help="Seconds between think-steps while pursuing a goal.")
    p.add_argument("--heartbeat", type=float, default=float(env("AGENT_HEARTBEAT", "0")),
                   help="If >0, auto-resume the last goal/job after this many idle seconds.")
    p.add_argument("--status-interval", type=float, default=float(env("AGENT_STATUS_INTERVAL", "30")),
                   help="Print a one-line status update every N seconds (0 = off).")
    p.add_argument("--narrate", action=argparse.BooleanOptionalAction,
                   default=env("AGENT_NARRATE", "1") not in ("0", "false", "False", "off"),
                   help="Announce activity changes in chat (default on; --no-narrate to disable).")
    p.add_argument("--max-bot-restarts", type=int, default=int(env("AGENT_MAX_BOT_RESTARTS", "20")),
                   help="Relaunch bot.js after a crash up to N times (0 = unlimited).")
    p.add_argument("--goal", default=None, help="Initial goal to pursue on startup.")
    p.add_argument("--no-warmup", action="store_true", help="Skip preloading the model at startup.")
    p.add_argument("--external-bot", action="store_true",
                   help="Don't spawn bot.js; connect to an already-running bridge (single bot only).")
    p.add_argument("--no-install", action="store_true", help="Skip auto 'npm install' in bot/.")
    p.add_argument("--list-models", action="store_true", help="List local Ollama models and exit.")
    args = p.parse_args()
    args.username_explicit = bool(args.username)
    if not args.username:
        args.username = f"ClaudeBot{random.randint(100, 999)}"  # unique-ish per run
    if args.bots < 1:
        args.bots = 1
    if args.external_bot and args.bots > 1:
        print("[warn] --external-bot supports a single bot; forcing --bot 1.", flush=True)
        args.bots = 1
    return args


def make_bot_names(cfg: argparse.Namespace) -> list[str]:
    """Distinct player names for each bot (MC needs unique names in a world)."""
    if cfg.bots == 1:
        return [cfg.username]
    if cfg.username_explicit:  # honor an explicit base by suffixing an index
        return [f"{cfg.username}{i + 1}"[:16] for i in range(cfg.bots)]
    names: list[str] = []
    seen: set[str] = set()
    while len(names) < cfg.bots:
        nm = f"ClaudeBot{random.randint(100, 999)}"
        if nm not in seen:
            seen.add(nm)
            names.append(nm)
    return names


class BotUnit:
    """One bot: its own config, control bridge, LLM agent, and child process."""

    def __init__(self, index: int, cfg: argparse.Namespace, bridge: BotBridge, agent: Agent, label: str):
        self.index = index
        self.cfg = cfg
        self.bridge = bridge
        self.agent = agent
        self.label = label
        self.proc_holder: dict = {"p": None}


async def ensure_bot_deps(cfg: argparse.Namespace) -> None:
    # Gate on the presence of the core package, not just the folder, so a broken
    # first install (which can leave node_modules half-populated) re-runs.
    if cfg.no_install or (BOT_DIR / "node_modules" / "mineflayer").is_dir():
        return
    npm = shutil.which("npm")
    if not npm:
        raise SystemExit("npm not found on PATH. Install Node.js >=22, then re-run.")
    print("[setup] installing bot dependencies (first run, one-time) ...", flush=True)
    proc = await asyncio.create_subprocess_shell("npm install", cwd=str(BOT_DIR))
    rc = await proc.wait()
    if rc != 0:
        print(f"[setup] 'npm install' exited {rc}. Core deps may still be usable; continuing.", flush=True)


async def spawn_bot(cfg: argparse.Namespace) -> asyncio.subprocess.Process:
    node = shutil.which("node")
    if not node:
        raise SystemExit("node not found on PATH. Install Node.js >=22, then re-run.")
    env = dict(os.environ)
    env.update({
        "MC_HOST": cfg.mc_host,
        "MC_PORT": str(cfg.mc_port),
        "MC_USERNAME": cfg.username,
        "MC_AUTH": cfg.auth,
        "MC_VERSION": cfg.mc_version or "",
        "MC_OWNER": cfg.owner or "",
        "BRIDGE_HOST": cfg.bridge_host,
        "BRIDGE_PORT": str(cfg.bridge_port),
    })
    # Isolate children from the console's Ctrl-C (we drive their lifecycle
    # explicitly); on POSIX a new session gives us a killable process group.
    kwargs: dict = {}
    if os.name == "nt":
        kwargs["creationflags"] = 0x00000200  # CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    proc = await asyncio.create_subprocess_exec(
        node, "--expose-gc", "bot.js", cwd=str(BOT_DIR), env=env,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, **kwargs,
    )
    _assign_to_job(proc.pid)  # OS-level guarantee the child dies with us
    return proc


async def pipe_output(proc: asyncio.subprocess.Process, label: str = "") -> None:
    assert proc.stdout is not None
    prefix = f"[{label}] " if label else ""
    while True:
        line = await proc.stdout.readline()
        if not line:
            break
        print(prefix + line.decode("utf-8", "replace").rstrip(), flush=True)


async def status_ticker(unit: BotUnit, shutdown: asyncio.Event, interval: float) -> None:
    """Print a one-line status update every `interval` seconds (0 = off)."""
    if interval <= 0:
        return
    bridge, agent = unit.bridge, unit.agent
    tag = f"[status {unit.label}]" if unit.label else "[status]"
    while not shutdown.is_set():
        try:
            await asyncio.wait_for(shutdown.wait(), timeout=interval)
            break  # shutdown fired
        except asyncio.TimeoutError:
            pass
        if not bridge.ready:
            print(f"{tag} offline — bot disconnected / trying to reconnect", flush=True)
            continue
        try:
            s = await bridge.send("state", timeout=10)
        except BotError:
            print(f"{tag} (state unavailable)", flush=True)
            continue
        pos = s.get("position") or {}
        ap = s.get("autopilot") or {}
        inv = s.get("inventory") or {}
        players = s.get("players") or []
        kind = "job" if agent.persistent else "goal"
        goal = agent.goal or "(idle)"
        goal = goal if len(goal) <= 40 else goal[:39] + "…"
        doing = "fighting" if ap.get("fighting") else ("fleeing" if ap.get("fleeing") else "ok")
        near = f"{players[0]['username']}@{round(players[0]['distance'])}m" if players else "none"
        print(
            f"{tag} {kind}: {goal} | pos ({int(pos.get('x', 0))},{int(pos.get('y', 0))},{int(pos.get('z', 0))}) "
            f"hp {s.get('health')}/20 food {s.get('food')}/20 | held {s.get('heldItem') or '-'} "
            f"| items {sum(inv.values())} | mem {s.get('memMB')}MB | nearest {near} | {doing}",
            flush=True,
        )


async def supervise_bot(unit: BotUnit, shutdown: asyncio.Event, max_restarts: int) -> None:
    """Spawn bot.js, connect the bridge, and relaunch it on crash so the session survives.

    Only CRASHES relaunch. A deliberate exit — code 0 (clean shutdown) or 1 (gave up
    reconnecting to MC / bridge port in use) — stops this bot instead, since relaunching
    wouldn't help. On a crash (e.g. OOM abort = 134) the goal/job (kept here in the Python
    agent) and the server-side position/inventory mean the bot resumes where it left off.

    Only the LAST bot standing triggers a full controller shutdown, so one bot exiting
    deliberately doesn't take the others down with it.
    """
    cfg, bridge, holder, label = unit.cfg, unit.bridge, unit.proc_holder, unit.label
    tag = f"[controller {label}]" if label else "[controller]"
    restarts_left = max_restarts
    while not shutdown.is_set():
        cfg.bridge_port = _find_free_port(cfg.bridge_host, cfg.bridge_port)
        bridge.host, bridge.port = cfg.bridge_host, cfg.bridge_port
        print(f"{tag} launching bot.js (bridge {cfg.bridge_host}:{cfg.bridge_port}) ...", flush=True)
        proc = await spawn_bot(cfg)
        holder["p"] = proc
        pipe_task = asyncio.create_task(pipe_output(proc, label))

        # Connect the bridge to this bot.js, aborting the wait if it dies during startup.
        connect_task = asyncio.create_task(bridge.connect())
        while not connect_task.done():
            if proc.returncode is not None or shutdown.is_set():
                connect_task.cancel()
                break
            await asyncio.sleep(0.25)
        try:
            await connect_task
        except (BotError, asyncio.CancelledError):
            pass

        rc = await proc.wait()  # block until bot.js exits
        pipe_task.cancel()
        try:
            await bridge.close()
        except Exception:
            pass

        if shutdown.is_set():
            break
        if rc in (0, 1):  # deliberate exit — not a crash
            print(f"{tag} bot.js exited (code {rc}) — not a crash; this bot is done.", flush=True)
            break
        if max_restarts > 0 and restarts_left <= 0:
            print(f"{tag} bot.js crashed (code {rc}) and hit the restart limit — this bot is done.", flush=True)
            break
        restarts_left -= 1
        left = "unlimited" if max_restarts <= 0 else restarts_left
        print(f"{tag} bot.js crashed (code {rc}) — relaunching in 3s; it rejoins where it left off "
              f"({left} restarts left).", flush=True)
        await asyncio.sleep(3)
    holder["p"] = None


def make_on_event(agent: Agent, label: str):
    """Per-bot bridge event handler that labels its output so N bots stay legible."""
    tag = f"[{label}] " if label else ""

    def on_event(event: str, data: dict) -> None:
        if event == "chat":
            print(f"{tag}[mc] <{data.get('username')}> {data.get('message')}", flush=True)
            agent.note_chat(data.get("username", "?"), data.get("message", ""))
        elif event == "spawn":
            caps = data.get("capabilities") or {}
            print(f"{tag}[controller] spawned as {data.get('username')} (v{data.get('version')}) "
                  f"[pvp={caps.get('pvp')}, collect={caps.get('collect')}]", flush=True)
        elif event == "auto":
            extra = {k: v for k, v in data.items() if k != "kind"}
            print(f"{tag}[auto] {data.get('kind')} {extra or ''}".rstrip(), flush=True)
        elif event in ("kicked", "end", "error", "death"):
            print(f"{tag}[mc] {event}: {data}", flush=True)

    return on_event


def start_stdin_reader(loop: asyncio.AbstractEventLoop, queue: asyncio.Queue) -> None:
    """Read stdin on a daemon thread (abandoned at exit, so it never blocks shutdown)."""
    def reader() -> None:
        try:
            for line in sys.stdin:
                loop.call_soon_threadsafe(queue.put_nowait, line)
        except Exception:
            pass
        loop.call_soon_threadsafe(queue.put_nowait, None)  # EOF sentinel
    threading.Thread(target=reader, daemon=True, name="stdin-reader").start()


async def _broadcast(units: list[BotUnit], action: str, **kw):
    """Send a bridge action to every ready bot; returns per-bot results/exceptions."""
    return await asyncio.gather(
        *[u.bridge.send(action, **kw) for u in units if u.bridge.ready],
        return_exceptions=True,
    )


async def console(units: list[BotUnit], llm: OllamaClient,
                  shutdown: asyncio.Event, cmd_queue: asyncio.Queue) -> None:
    print_help(len(units))
    agents = [u.agent for u in units]
    while not shutdown.is_set():
        get_task = asyncio.ensure_future(cmd_queue.get())
        stop_task = asyncio.ensure_future(shutdown.wait())
        done, _ = await asyncio.wait({get_task, stop_task}, return_when=asyncio.FIRST_COMPLETED)
        if get_task not in done:
            get_task.cancel()
            break
        stop_task.cancel()
        line = get_task.result()
        if line is None:  # EOF
            shutdown.set()
            break
        line = line.strip()
        if not line:
            continue
        verb, _, rest = line.partition(" ")
        rest = rest.strip()
        v = verb.lower().lstrip("/")  # tolerate a leading slash, e.g. /model
        try:
            if v in ("quit", "exit"):
                shutdown.set()
                break
            elif v == "help":
                print_help(len(units))
            elif v == "goal":
                for a in agents:
                    a.set_goal(rest)
                print(f"[you] goal set for all: {rest!r}" if rest else "[you] goal cleared")
            elif v == "job":
                parts = rest.split(None, 1)
                if not parts:
                    print("[you] jobs: " + ", ".join(JOB_PRESETS) + "  (or: job <free text>)")
                else:
                    key = parts[0].lower()
                    arg = parts[1].strip() if len(parts) > 1 else ""
                    if key in JOB_PRESETS:
                        if key == "defend" and arg:
                            await _broadcast(units, "setOwner", username=arg, timeout=15)
                        directive = JOB_PRESETS[key].replace("{arg}", arg or "the nearest player")
                        for a in agents:
                            a.set_job(directive)
                        print(f"[you] job set for all: {key} {arg}".rstrip())
                    else:
                        for a in agents:
                            a.set_job(rest)
                        print(f"[you] job set for all (custom): {rest!r}")
            elif v == "reflex":
                parts = rest.split()
                keymap = {"eat": "autoEat", "defend": "autoDefend", "pickup": "autoPickup",
                          "wander": "idleWander", "greet": "greet"}
                if len(parts) >= 2 and parts[0] in ("on", "off"):
                    k = keymap.get(parts[1], parts[1])
                    await _broadcast(units, "setReflexes", timeout=15, **{k: parts[0] == "on"})
                    print(f"[you] reflex {k} -> {parts[0]} (all bots)")
                else:
                    for u in units:
                        if not u.bridge.ready:
                            continue
                        ap = (await u.bridge.send("state", timeout=15)).get("autopilot", {})
                        print(f"[{u.label or 'bot'}] " + json.dumps(ap))
            elif v == "owner":
                await _broadcast(units, "setOwner", username=rest, timeout=15)
                print(f"[you] owner set for all: {rest!r}")
            elif v == "heartbeat":
                try:
                    hb = max(0.0, float(rest)) if rest else 0.0
                    for a in agents:
                        a.heartbeat = hb
                    print(f"[you] heartbeat = {hb}s" + (" (off)" if hb <= 0 else ""))
                except ValueError:
                    print("[error] usage: heartbeat <seconds>  (0 = off)")
            elif v == "narrate":
                if rest.lower() in ("off", "0", "false", "no"):
                    val = False
                elif rest.lower() in ("on", "1", "true", "yes"):
                    val = True
                else:
                    val = not agents[0].narrate
                for a in agents:
                    a.narrate = val
                print(f"[you] narration {'on' if val else 'off'} (all bots)")
            elif v == "say":
                await _broadcast(units, "chat", message=rest, timeout=15)
            elif v == "stop":
                for a in agents:
                    a.set_goal(None)
                await _broadcast(units, "stop", timeout=15)
                print("[you] stopped (all bots)")
            elif v == "state":
                for u in units:
                    if not u.bridge.ready:
                        print(f"[{u.label or 'bot'}] (offline)")
                        continue
                    print(f"[{u.label or 'bot'}] " + json.dumps(await u.bridge.send("state", timeout=15)))
            elif v in ("model", "models"):
                models = await llm.list_models()
                if not models:
                    print("[model] no models found on the server")
                elif not rest:
                    print(f"[model] {llm.url}  (current: {llm.model})")
                    for i, m in enumerate(models, 1):
                        print(f"   {i:>2}  {m}" + ("   <- current" if m == llm.model else ""))
                    print("Switch with:  model <number|name>  (applies to all bots)")
                else:
                    matches = [m for m in models if m.startswith(rest)]
                    chosen = None
                    if rest.isdigit() and 1 <= int(rest) <= len(models):
                        chosen = models[int(rest) - 1]
                    elif rest in models:
                        chosen = rest
                    elif len(matches) == 1:
                        chosen = matches[0]
                    if chosen:
                        llm.model = chosen
                        print(f"[model] switched to {chosen} — warming up ...")
                        ok = await llm.warmup()
                        print(f"[model] {chosen} ready." if ok else f"[model] {chosen} selected (warmup failed).")
                    elif len(matches) > 1:
                        print(f"[model] ambiguous '{rest}': {', '.join(matches)}")
                    else:
                        print(f"[model] no match for {rest!r} — type `model` to list.")
            else:
                # Anything else is treated as a goal (for all bots), for convenience.
                for a in agents:
                    a.set_goal(line)
                print(f"[you] goal set for all: {line!r}")
        except (BotError, OllamaError) as e:
            print(f"[error] {e}")


def print_help(n_bots: int = 1) -> None:
    scope = f"  (commands apply to all {n_bots} bots)" if n_bots > 1 else ""
    print(
        f"\nConsole commands:{scope}\n"
        "  goal <text>        one-off task (or just type the text)\n"
        "  job <name|text>    standing job that never times out:\n"
        "                       guard | patrol | progress | play | harvest | stash | lumberjack | miner | defend <player> | gather <block>\n"
        "  reflex on|off <x>  toggle a reflex: eat|defend|pickup|wander|greet  (no arg = show autopilot)\n"
        "  owner <player>     set who the bot protects / flees toward\n"
        "  heartbeat <secs>   auto-resume the last goal/job after N idle seconds (0 = off)\n"
        "  narrate on|off     announce activity changes in chat\n"
        "  say <text>         make the bot say something in chat right now\n"
        "  stop               clear the goal/job and halt movement/combat\n"
        "  state              print the current world observation\n"
        "  model [n|name]     list the server's models / switch the active one live\n"
        "  help               show this help\n"
        "  quit               exit (stops every bot)\n"
        "You can also just talk to the bots in-game chat.\n",
        flush=True,
    )


async def terminate_all(units: list[BotUnit]) -> None:
    """Terminate every bot.js child, escalating to kill if it doesn't exit promptly."""
    procs = [u.proc_holder.get("p") for u in units]
    procs = [p for p in procs if p is not None and p.returncode is None]
    for p in procs:
        try:
            p.terminate()
        except ProcessLookupError:
            pass
    if not procs:
        return
    try:
        await asyncio.wait_for(
            asyncio.gather(*[p.wait() for p in procs], return_exceptions=True), timeout=5)
    except asyncio.TimeoutError:
        for p in procs:
            if p.returncode is None:
                try:
                    p.kill()
                except ProcessLookupError:
                    pass


async def main() -> None:
    cfg = parse_args()
    llm = OllamaClient(cfg.ollama_url, cfg.model, temperature=cfg.temperature)

    if cfg.list_models:
        try:
            print("\n".join(await llm.list_models()))
        except OllamaError as e:
            print(e)
        return

    # 1) Verify the model exists (warn, don't hard-fail).
    try:
        models = await llm.list_models()
        ver = await llm.version()
        if ver.startswith("0.5"):
            print(f"[warn] Ollama {ver} at {cfg.ollama_url} doesn't strictly enforce JSON schemas; "
                  "the agent leans on the prompt instead (works, but a newer Ollama is more reliable).")
        if cfg.model not in models:
            print(f"[warn] model '{cfg.model}' not found in Ollama. Available: {', '.join(models) or '(none)'}")
            print(f"[warn] pull it with:  ollama pull {cfg.model}")
        elif not cfg.no_warmup:
            print(f"[controller] warming up {cfg.model} (first load can take a bit) ...", flush=True)
            ok = await llm.warmup()
            print("[controller] model ready." if ok else "[warn] warmup failed; first decision may be slow.", flush=True)
    except OllamaError as e:
        print(f"[warn] could not reach Ollama at {cfg.ollama_url}: {e}")

    _win_job_setup()  # arm the child-cleanup Job Object before spawning anything
    shutdown = asyncio.Event()
    loop = asyncio.get_running_loop()

    # Build one BotUnit per bot: distinct name + distinct base bridge port.
    names = make_bot_names(cfg)
    units: list[BotUnit] = []
    for i, name in enumerate(names):
        ucfg = argparse.Namespace(**vars(cfg))
        ucfg.username = name
        # Give each bot its own 20-port band (matching _find_free_port's scan
        # window) so a busy base port can't bump one bot into another's range.
        ucfg.bridge_port = cfg.bridge_port + i * 20
        bridge = BotBridge(ucfg.bridge_host, ucfg.bridge_port)
        agent = Agent(bridge, llm, username=name, tick=cfg.tick,
                      heartbeat=cfg.heartbeat, narrate=cfg.narrate)
        label = name if cfg.bots > 1 else ""
        bridge.on_event(make_on_event(agent, label))
        units.append(BotUnit(i, ucfg, bridge, agent, label))

    # Once children exist, Ctrl-C must go through our graceful path (which kills them).
    install_signal_handlers(loop, shutdown, [u.proc_holder for u in units])

    print(f"[controller] {len(units)} bot(s): {', '.join(u.cfg.username for u in units)}  "
          f"(owner: {cfg.owner or 'none'})", flush=True)

    tasks: list[asyncio.Task] = []
    supervisor_tasks: list[asyncio.Task] = []
    try:
        if cfg.external_bot:
            print(f"[controller] connecting to external bot bridge "
                  f"{units[0].cfg.bridge_host}:{units[0].cfg.bridge_port} ...", flush=True)
            await units[0].bridge.connect()
        else:
            await ensure_bot_deps(cfg)
            for u in units:
                t = asyncio.create_task(supervise_bot(u, shutdown, cfg.max_bot_restarts))
                supervisor_tasks.append(t)
                tasks.append(t)

        # Wait (briefly) for spawns so the first goal doesn't fire into a void.
        print("[controller] waiting for bots to spawn (is your world/server running?) ...", flush=True)
        for _ in range(120):
            if shutdown.is_set() or all(u.bridge.ready for u in units):
                break
            await asyncio.sleep(0.5)
        up = sum(1 for u in units if u.bridge.ready)
        if up < len(units) and not shutdown.is_set():
            print(f"[controller] {up}/{len(units)} up — the rest keep trying. Open your world to LAN "
                  "(set --mc-port to the port it prints), or start a server.", flush=True)

        cmd_queue: asyncio.Queue = asyncio.Queue()
        start_stdin_reader(loop, cmd_queue)
        for u in units:
            if cfg.goal:
                u.agent.set_goal(cfg.goal)
            tasks.append(asyncio.create_task(u.agent.run()))
            tasks.append(asyncio.create_task(status_ticker(u, shutdown, cfg.status_interval)))
        tasks.append(asyncio.create_task(console(units, llm, shutdown, cmd_queue)))

        # Run until the user quits (shutdown) or every bot's supervisor has exited
        # on its own (all bots done/crashed out) — then there's nothing left to drive.
        await _wait_until_done(shutdown, supervisor_tasks)
    finally:
        print("[controller] shutting down — stopping all bots ...", flush=True)
        for u in units:
            u.agent.stop()
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for u in units:
            try:
                await u.bridge.close()
            except Exception:
                pass
        await terminate_all(units)
        # Closing the Job Object handle at interpreter exit reaps any stragglers.


async def _wait_until_done(shutdown: asyncio.Event, supervisor_tasks: list[asyncio.Task]) -> None:
    """Return when the user asks to quit, or when every bot supervisor has finished
    (all bots exited deliberately / gave up). Watches task completion, not the proc
    holders, so it can't misfire during the pre-spawn startup window."""
    if not supervisor_tasks:  # e.g. --external-bot
        await shutdown.wait()
        return
    while not shutdown.is_set():
        if all(t.done() for t in supervisor_tasks):
            print("[controller] all bots have exited — shutting down.", flush=True)
            shutdown.set()
            return
        try:
            await asyncio.wait_for(shutdown.wait(), timeout=1.0)
        except asyncio.TimeoutError:
            pass


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[controller] interrupted.")
