"""controller.py — the entrypoint.

Spawns the Node Mineflayer bot (bot.js), connects to its control bridge, wires
Minecraft chat into the LLM agent, and gives you a small console to set goals.

Run:  python controller.py --goal "follow me and say hi"
Everything is configurable by flag or environment variable; see --help.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import shutil
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


def _find_free_port(host: str, start: int, limit: int = 20) -> int:
    """First bindable TCP port at/after `start` — lets a 2nd bot auto-pick a bridge port."""
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
    p.add_argument("--username", default=env("MC_USERNAME", ""),
                   help="Bot player name (unique per bot). Blank = random ClaudeBot###.")
    p.add_argument("--auth", default=env("MC_AUTH", "offline"), choices=["offline", "microsoft"])
    p.add_argument("--owner", default=env("MC_OWNER", ""), help="Player the bot protects / flees toward.")
    p.add_argument("--mc-version", default=env("MC_VERSION", ""),
                   help="Blank = auto-detect (recommended). If pinning, use a supported anchor (e.g. 1.21.8).")
    p.add_argument("--bridge-host", default=env("BRIDGE_HOST", "127.0.0.1"))
    p.add_argument("--bridge-port", type=int, default=int(env("BRIDGE_PORT", "25585")))
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
                   help="Don't spawn bot.js; connect to an already-running bridge.")
    p.add_argument("--no-install", action="store_true", help="Skip auto 'npm install' in bot/.")
    p.add_argument("--list-models", action="store_true", help="List local Ollama models and exit.")
    args = p.parse_args()
    if not args.username:
        args.username = f"ClaudeBot{random.randint(100, 999)}"  # unique-ish per run
    return args


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
    return await asyncio.create_subprocess_exec(
        node, "--expose-gc", "bot.js", cwd=str(BOT_DIR), env=env,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )


async def pipe_output(proc: asyncio.subprocess.Process) -> None:
    assert proc.stdout is not None
    while True:
        line = await proc.stdout.readline()
        if not line:
            break
        print(line.decode("utf-8", "replace").rstrip(), flush=True)


async def status_ticker(bridge: BotBridge, agent: Agent, shutdown: asyncio.Event, interval: float) -> None:
    """Print a one-line status update every `interval` seconds (0 = off)."""
    if interval <= 0:
        return
    while not shutdown.is_set():
        try:
            await asyncio.wait_for(shutdown.wait(), timeout=interval)
            break  # shutdown fired
        except asyncio.TimeoutError:
            pass
        if not bridge.ready:
            print("[status] offline — bot disconnected / trying to reconnect", flush=True)
            continue
        try:
            s = await bridge.send("state", timeout=10)
        except BotError:
            print("[status] (state unavailable)", flush=True)
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
            f"[status] {kind}: {goal} | pos ({int(pos.get('x', 0))},{int(pos.get('y', 0))},{int(pos.get('z', 0))}) "
            f"hp {s.get('health')}/20 food {s.get('food')}/20 | held {s.get('heldItem') or '-'} "
            f"| items {sum(inv.values())} | mem {s.get('memMB')}MB | nearest {near} | {doing}",
            flush=True,
        )


async def supervise_bot(cfg: argparse.Namespace, bridge: BotBridge, shutdown: asyncio.Event,
                        holder: dict, max_restarts: int) -> None:
    """Spawn bot.js, connect the bridge, and relaunch it on crash so the session survives.

    Only CRASHES relaunch. A deliberate exit — code 0 (clean shutdown) or 1 (gave up
    reconnecting to MC / bridge port in use) — shuts the controller down instead, since
    relaunching wouldn't help. On a crash (e.g. OOM abort = 134) the goal/job (kept here
    in the Python agent) and the server-side position/inventory mean the bot resumes.
    """
    restarts_left = max_restarts
    while not shutdown.is_set():
        cfg.bridge_port = _find_free_port(cfg.bridge_host, cfg.bridge_port)
        bridge.host, bridge.port = cfg.bridge_host, cfg.bridge_port
        print(f"[controller] launching bot.js (bridge {cfg.bridge_host}:{cfg.bridge_port}) ...", flush=True)
        proc = await spawn_bot(cfg)
        holder["p"] = proc
        pipe_task = asyncio.create_task(pipe_output(proc))

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
            print(f"[controller] bot.js exited (code {rc}) — not a crash; shutting down.", flush=True)
            shutdown.set()
            break
        if max_restarts > 0 and restarts_left <= 0:
            print(f"[controller] bot.js crashed (code {rc}) and hit the restart limit — shutting down.", flush=True)
            shutdown.set()
            break
        restarts_left -= 1
        left = "unlimited" if max_restarts <= 0 else restarts_left
        print(f"[controller] bot.js crashed (code {rc}) — relaunching in 3s; it rejoins where it left off "
              f"({left} restarts left).", flush=True)
        await asyncio.sleep(3)
    holder["p"] = None


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


async def console(agent: Agent, bridge: BotBridge, llm: OllamaClient,
                  shutdown: asyncio.Event, cmd_queue: asyncio.Queue) -> None:
    print_help()
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
                print_help()
            elif v == "goal":
                agent.set_goal(rest)
                print(f"[you] goal set: {rest!r}" if rest else "[you] goal cleared")
            elif v == "job":
                parts = rest.split(None, 1)
                if not parts:
                    print("[you] jobs: " + ", ".join(JOB_PRESETS) + "  (or: job <free text>)")
                else:
                    key = parts[0].lower()
                    arg = parts[1].strip() if len(parts) > 1 else ""
                    if key in JOB_PRESETS:
                        if key == "defend" and arg:
                            await bridge.send("setOwner", username=arg, timeout=15)
                        agent.set_job(JOB_PRESETS[key].replace("{arg}", arg or "the nearest player"))
                        print(f"[you] job set: {key} {arg}".rstrip())
                    else:
                        agent.set_job(rest)
                        print(f"[you] job set (custom): {rest!r}")
            elif v == "reflex":
                parts = rest.split()
                keymap = {"eat": "autoEat", "defend": "autoDefend", "pickup": "autoPickup",
                          "wander": "idleWander", "greet": "greet"}
                if len(parts) >= 2 and parts[0] in ("on", "off"):
                    k = keymap.get(parts[1], parts[1])
                    await bridge.send("setReflexes", timeout=15, **{k: parts[0] == "on"})
                    print(f"[you] reflex {k} -> {parts[0]}")
                else:
                    ap = (await bridge.send("state", timeout=15)).get("autopilot", {})
                    print(json.dumps(ap, indent=2))
            elif v == "owner":
                await bridge.send("setOwner", username=rest, timeout=15)
                print(f"[you] owner set: {rest!r}")
            elif v == "heartbeat":
                try:
                    agent.heartbeat = max(0.0, float(rest)) if rest else 0.0
                    print(f"[you] heartbeat = {agent.heartbeat}s" + (" (off)" if agent.heartbeat <= 0 else ""))
                except ValueError:
                    print("[error] usage: heartbeat <seconds>  (0 = off)")
            elif v == "narrate":
                if rest.lower() in ("off", "0", "false", "no"):
                    agent.narrate = False
                elif rest.lower() in ("on", "1", "true", "yes"):
                    agent.narrate = True
                else:
                    agent.narrate = not agent.narrate
                print(f"[you] narration {'on' if agent.narrate else 'off'}")
            elif v == "say":
                await bridge.send("chat", message=rest, timeout=15)
            elif v == "stop":
                agent.set_goal(None)
                await bridge.send("stop", timeout=15)
                print("[you] stopped")
            elif v == "state":
                print(json.dumps(await bridge.send("state", timeout=15), indent=2))
            elif v in ("model", "models"):
                models = await llm.list_models()
                if not models:
                    print("[model] no models found on the server")
                elif not rest:
                    print(f"[model] {llm.url}  (current: {llm.model})")
                    for i, m in enumerate(models, 1):
                        print(f"   {i:>2}  {m}" + ("   <- current" if m == llm.model else ""))
                    print("Switch with:  model <number|name>")
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
                # Anything else is treated as a goal, for convenience.
                agent.set_goal(line)
                print(f"[you] goal set: {line!r}")
        except (BotError, OllamaError) as e:
            print(f"[error] {e}")


def print_help() -> None:
    print(
        "\nConsole commands:\n"
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
        "  quit               exit\n"
        "You can also just talk to the bot in-game chat.\n",
        flush=True,
    )


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

    print(f"[controller] bot name: {cfg.username}  (owner: {cfg.owner or 'none'})", flush=True)
    bridge = BotBridge(cfg.bridge_host, cfg.bridge_port)
    shutdown = asyncio.Event()
    agent = Agent(bridge, llm, username=cfg.username, tick=cfg.tick,
                  heartbeat=cfg.heartbeat, narrate=cfg.narrate)
    proc_holder: dict = {"p": None}
    tasks: list[asyncio.Task] = []

    def on_event(event: str, data: dict) -> None:
        if event == "chat":
            print(f"[mc] <{data.get('username')}> {data.get('message')}", flush=True)
            agent.note_chat(data.get("username", "?"), data.get("message", ""))
        elif event == "spawn":
            caps = data.get("capabilities") or {}
            print(f"[controller] bot spawned as {data.get('username')} (v{data.get('version')}) "
                  f"[pvp={caps.get('pvp')}, collect={caps.get('collect')}]", flush=True)
        elif event == "auto":
            extra = {k: v for k, v in data.items() if k != "kind"}
            print(f"[auto] {data.get('kind')} {extra or ''}".rstrip(), flush=True)
        elif event in ("kicked", "end", "error", "death"):
            print(f"[mc] {event}: {data}", flush=True)

    bridge.on_event(on_event)

    try:
        # Bring up the Node bot. The supervisor spawns it, connects the bridge, and
        # relaunches on crash so the session survives — the goal/job lives here in Python
        # and the MC server restores position/inventory, so the bot resumes where it left off.
        if cfg.external_bot:
            print(f"[controller] connecting to external bot bridge {cfg.bridge_host}:{cfg.bridge_port} ...", flush=True)
            await bridge.connect()
        else:
            await ensure_bot_deps(cfg)
            tasks.append(asyncio.create_task(
                supervise_bot(cfg, bridge, shutdown, proc_holder, cfg.max_bot_restarts)))

        # Wait (briefly) for the first spawn so the first goal doesn't fire into a void.
        print("[controller] waiting for the bot to spawn (is your world/server running?) ...", flush=True)
        for _ in range(120):
            if bridge.ready or shutdown.is_set():
                break
            await asyncio.sleep(0.5)
        if not bridge.ready and not shutdown.is_set():
            print("[controller] not spawned yet — it'll keep trying. Open your world to LAN "
                  "(set --mc-port to the port it prints), or start a server.", flush=True)

        if cfg.goal:
            agent.set_goal(cfg.goal)

        cmd_queue: asyncio.Queue = asyncio.Queue()
        start_stdin_reader(asyncio.get_running_loop(), cmd_queue)
        tasks.append(asyncio.create_task(agent.run()))
        tasks.append(asyncio.create_task(console(agent, bridge, llm, shutdown, cmd_queue)))
        tasks.append(asyncio.create_task(status_ticker(bridge, agent, shutdown, cfg.status_interval)))

        await shutdown.wait()
    finally:
        print("[controller] shutting down ...", flush=True)
        agent.stop()
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await bridge.close()
        p = proc_holder.get("p")
        if p and p.returncode is None:
            try:
                p.terminate()
                await asyncio.wait_for(p.wait(), timeout=5)
            except (ProcessLookupError, asyncio.TimeoutError):
                try:
                    p.kill()
                except ProcessLookupError:
                    pass


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[controller] interrupted.")
