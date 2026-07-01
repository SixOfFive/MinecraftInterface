# Minecraft ⇄ Local LLM

A local LLM (via [Ollama](https://ollama.com)) plays a **Minecraft Java Edition**
character. The model receives a JSON view of the world each tick and issues one
high‑level action — walk, mine, place, follow, fight, chat — pursuing whatever
goal you give it and talking to players in in‑game chat.

```
Minecraft Java server  ⇄  bot/bot.js  (Mineflayer "hands": pathfinding, mining, combat)
                              ⇅  newline-delimited JSON over a local TCP socket
                          controller.py → agent.py  (the "brain": observe → decide → act)
                              ⇅  HTTP  (/api/chat, grammar-constrained JSON)
                          Ollama  (qwen2.5:7b, etc.)  — chooses the next action
```

The **brain is 100% Python standard library** (no `pip install` needed). The
**hands** are a thin ~350‑line Node/Mineflayer process, because Mineflayer is the
only library that gives a Java‑Edition bot real physics, pathfinding, and combat —
there is no pure‑Python equivalent. All decision‑making lives in Python; `bot.js`
just executes one low‑level action per request.

---

## Prerequisites

| Need | This machine |
|------|--------------|
| **Node.js ≥ 22** (Mineflayer 4.37 requires it) | ✅ v24 |
| **Python 3.10+** (tested on 3.14) | ✅ 3.14 |
| **Ollama** running with an instruct model | e.g. `qwen2.5:7b` (default `localhost:11434`) |
| **Minecraft Java Edition** on a supported version (**1.21.11**, newest Mineflayer supports) | install 1.21.11 |

No Python packages are required. Node dependencies install automatically on first
run (or `cd bot && npm install`).

---

## Give the bot a world to join

> **⚠ Version:** Install Minecraft **1.21.11** (Launcher → *Installations* → *New installation* →
> version `release 1.21.11`) — the newest version Mineflayer 4.37 supports. It does **not** yet
> support the new `26.x` numbered releases (26.1/26.2, protocol 775/776) — a 26.x world rejects
> the bot with *"unsupported protocol version"*, and you can't force an older client on it. When
> PrismarineJS adds 26.x support, `cd bot && npm update` and you can use the latest.


The bot joins as a **second player**, so you need a server it can connect to. Two
easy paths, both **offline‑mode** (no Microsoft auth for the bot):

**A. Open a single‑player world to LAN (fastest).**
1. Load a single‑player world → `Esc` → **Open to LAN** → **Start LAN World**.
2. The chat prints `Local game hosted on port NNNNN`. **Note that port.**
3. LAN worlds are offline‑mode, so the bot joins with `auth: offline`. ⚠️ The port
   **changes every time** you re‑open to LAN — pass it with `--mc-port NNNNN`.
4. When Windows prompts, **allow Java through the firewall**. Same PC as
   Minecraft → keep `--mc-host 127.0.0.1`; a **different** machine → pass that PC's
   LAN IP (e.g. `--mc-host 192.168.1.50`) and allow the LAN port through the firewall.

**B. Run a dedicated offline server (stable, persistent).**
Use Paper/Fabric/vanilla with `server.properties` → `online-mode=false`, default
port `25565`. Best if you want the bot always available at a fixed address.

> The bot's default name is `ClaudeBot`. Each bot needs a **unique** name in
> offline mode, or the server kicks the duplicate.

---

## Run it

```powershell
# from the project root
python controller.py
```

First run auto‑installs the Node deps, spawns `bot.js`, connects, and waits for the
bot to spawn in your world. Typical Open‑to‑LAN start:

```powershell
python controller.py --mc-port 49876 --goal "come to me and say hello"
```

Then drive it from the **console** (or just talk to it in Minecraft chat):

```
goal <text>        one-off task (or just type the text)
job <name|text>    STANDING job that never times out:
                     guard | patrol | harvest | stash | lumberjack | miner | defend <player> | gather <block>
reflex on|off <x>  toggle a reflex: eat|defend|pickup|wander|greet   (no arg = show autopilot)
owner <player>     set who the bot protects / flees toward
say <text>         speak in chat right now
stop               clear the goal/job and halt movement/combat
state              print the current world observation the LLM sees
model [n|name]     list the server's models / switch the active one live
quit               exit
```

Anything you type that isn't a command becomes the goal. In‑game, just chat at the
bot — messages wake it up and it decides how to respond.

## Autonomy — leaving it alone

Two layers keep it useful while you're not watching:

**Reflexes** run in `bot.js` at ~2 Hz, independent of the (slow) LLM, so survival never
waits on a think-cycle:

- **auto-eat** when hunger drops (`EAT_AT`, default 16)
- **auto-defend** — attacks hostiles within `DEFEND_RADIUS` (default 6) or when hit
- **flee** to safety (toward your `--owner` if set) at low health (`FLEE_HEALTH`, default 6)
- **auto-pickup** nearby drops, plus **idle life**: wanders a little and greets players when idle

Toggle any live with `reflex on|off eat|defend|pickup|wander|greet`, or via env
(`AUTO_EAT`, `AUTO_DEFEND`, `AUTO_PICKUP`, `IDLE_WANDER`, `GREET`, `DEFEND_RADIUS`,
`FLEE_HEALTH`, `EAT_AT`). The LLM sees an `autopilot` block in the observation and stands
aside while a reflex handles a threat.

**Jobs** are standing directives that never time out (one-off goals end after ~40 steps).
Set one and walk away:

```
job guard              # hold this spot, kill anything that approaches
job patrol             # roam nearby and clear hostiles
job lumberjack         # chop + collect wood forever
job miner              # dig ore/stone + collect forever
job defend Steve       # follow + protect a player (also sets them as owner)
job gather cobblestone # mine + collect a specific block forever
```

**Anti-loop guards** watch every action: if one keeps failing (e.g. placing into a solid
block), the agent injects a "you already tried X — do something different" hint; if it stays
stuck it hard-stops, says so in chat, and pauses — so a confused model can't burn cycles
hammering the same dead end.

## Running more than one bot

Each bot is its own `controller.py`. To add a second, just give it a **unique name**
(offline mode rejects duplicate usernames) — the bridge port is picked automatically:

```powershell
python controller.py --mc-port <port> --username ClaudeBot2 --owner Steve
```

All bots share the one Ollama endpoint (requests queue), so more bots = slightly slower
per-decision when several think at once.

## Keeping it going: jobs vs heartbeat

- **Jobs never time out** — `job guard` runs until you stop it. This is the right tool for
  "guard my base all night."
- **One-off `goal`s** end after ~40 steps. To make any goal auto-resume when the bot goes
  idle, set a **heartbeat**: `--heartbeat 30` at launch (or `heartbeat 30` in the console)
  re-issues the last goal/job after 30 idle seconds. An explicit `stop` clears it.

---

## Configuration

Every flag has an environment‑variable fallback, and machine‑specific settings go in a
**gitignored `.env`** (copy `.env.example` → `.env`) — that's where your Ollama endpoint,
model, and owner name live, so they never get committed.

**Changing the model is one line:** edit `OLLAMA_MODEL` in `.env` — that's the single place
you set which model the bots think with. (Nothing else hardcodes a model except one
last‑resort fallback constant, `DEFAULT_MODEL` in `ollama_client.py`.)

| Flag | Env | Default | Meaning |
|------|-----|---------|---------|
| `--mc-host` | `MC_HOST` | `127.0.0.1` | Server host |
| `--mc-port` | `MC_PORT` | `25565` | Server port (the LAN port for Open‑to‑LAN) |
| `--username` | `MC_USERNAME` | `ClaudeBot` | Bot player name (unique per bot) |
| `--owner` | `MC_OWNER` | *(none)* | Player the bot protects / flees toward |
| `--auth` | `MC_AUTH` | `offline` | `offline` or `microsoft` |
| `--mc-version` | `MC_VERSION` | *(auto)* | Blank = auto‑detect. If pinning, use a supported **anchor** (see below) |
| `--model` | `OLLAMA_MODEL` | `qwen2.5:7b` | Ollama model (try `ministral-3:14b` for smarter, slower play) |
| `--ollama-url` | `OLLAMA_URL` | `http://localhost:11434` | Ollama endpoint (set yours in `.env`) |
| `--temperature` | `OLLAMA_TEMP` | `0.3` | LLM sampling temperature |
| `--tick` | `AGENT_TICK` | `2.0` | Seconds between think‑steps while chasing a goal |
| `--heartbeat` | `AGENT_HEARTBEAT` | `0` (off) | Auto‑resume the last goal/job after N idle seconds |
| `--status-interval` | `AGENT_STATUS_INTERVAL` | `30` | Print a one‑line status update every N seconds (0 = off) |
| `--bridge-port` | `BRIDGE_PORT` | `25585` | Local TCP port between Python and `bot.js` (auto‑bumps if busy) |
| `--external-bot` | — | off | Don't spawn `bot.js`; connect to one you started |
| `--list-models` | — | — | Print local Ollama models and exit |

Node‑side extras (env only): `MC_VIEW_DISTANCE` (`tiny`/`short`/`normal`/`far` — default
`tiny` to keep chunk memory low), `MOVE_CAN_DIG` (default `false`; `true` lets the bot tunnel
to buried resources but can blow up pathfinder memory), `PATHFINDER_TIMEOUT_MS` (A* time cap,
default 4000), `MC_AUTO_RECONNECT`, `MC_RECONNECT_MS`, `MC_MAX_RECONNECT`
(give up + exit after N failed reconnects so the controller can terminate; `0` = retry forever),
and the reflex knobs
(`AUTO_EAT`, `AUTO_DEFEND`, `AUTO_PICKUP`, `IDLE_WANDER`, `GREET`, `EAT_AT`,
`DEFEND_RADIUS`, `FLEE_HEALTH`) — see **Autonomy** above.

---

## Actions the LLM can take

`none`, `chat`, `goto {x,y,z}`, `gotoPlayer {username}`, `follow {username}`,
`stop`, `lookAt {x,y,z}`, `mine {name,count}`, `place {name,x,y,z}`,
`collect`, `harvestNearest {count?}`, `stashResources`, `attack {target}`,
`equip {name}`, `drop {name,count}`, `eat {name?}`, `flee {target?}`, `sleep`,
`craft {name,count}`, `depositChest {name,count}`, `withdrawChest {name,count}`.

`harvestNearest` and `stashResources` are the reliable way to gather + store — they do
the find/walk/mine/collect (or walk/deposit) in one call, so a small model doesn't have to
chain the steps itself.

The model is **grammar‑constrained** by a JSON Schema (Ollama's `format` field), so
its output is always a valid action object — no markdown fences or stray prose.

---

## Troubleshooting

- **`unsupported protocol version` / won't connect** — your Minecraft is newer than
  Mineflayer 4.37 supports (up to **1.21.11**). Either update Mineflayer
  (`cd bot && npm update`) or run the world on a supported version. If you pin
  `--mc-version`, use a supported **anchor**: for a 1.21.7 server pass `1.21.8`; for
  1.21.10 pass `1.21.9`; for 1.21.2 pass `1.21.3`. Leaving it blank (auto) usually
  picks the right one by protocol number.
- **Bot never spawns** — the world/server isn't reachable. For Open‑to‑LAN, re‑check
  the port from chat (it changes each time) and pass `--mc-port`. Confirm the world
  is actually open to LAN.
- **`You logged in from another location` / instant kick** — another player/bot uses
  the same name. Pass a unique `--username`.
- **`model ... not found`** — `ollama pull qwen2.5:7b` (or point `--model` at one
  from `--list-models`, or switch live with the `model` console command). Good picks:
  `qwen2.5:7b` (fast, reliable), `qwen2.5:14b-instruct` or `ministral-3:14b` (smarter, slower),
  `qwen3:4b` (smallest/fastest, weaker planner).
- **Mining a block drops nothing** — it needs the right tool (ores need a pickaxe,
  etc.). Put a tool in the bot's inventory; `mineflayer-collectblock` auto‑equips it.
- **Bot digs through your builds to reach a goal** — set `MOVE_CAN_DIG=false`.
- **`player not visible`** — the target player is out of the bot's tracking range;
  the bot only knows about entities the server has sent it. If it happens a lot, raise
  `MC_VIEW_DISTANCE` (at the cost of more memory).
- **Bot crashes with `JavaScript heap out of memory` (exit 134)** — usually the pathfinder:
  with `MOVE_CAN_DIG=true`, searching a path to a buried block (e.g. `mine stone`) explodes the
  A* search into GBs. Keep `MOVE_CAN_DIG=false` (the default) so it paths over terrain, and
  `MC_VIEW_DISTANCE=tiny`. Watch the `mem NNNMB` field in the status line — it should stay
  low/flat, not climb toward 4000. (`reflex off wander` also helps idle bots stop loading chunks.)

---

## How it works (the loop)

1. `controller.py` spawns `bot.js`, which connects to Minecraft and opens a local
   TCP control socket.
2. When idle (no goal, no chat) the agent makes **no** LLM calls. A goal or an
   in‑game chat message wakes it.
3. Each step: fetch the world `state` → send it + the goal + recent history to the
   local model → get one JSON action back → execute it via the bridge → record the
   result → repeat every `--tick` seconds until the model marks the goal complete.

## Extending it

Add an action in two places: a handler in `bot/bot.js` (`ACTIONS = { ... }`) and its
name + fields in `agent.py` (`ACTION_NAMES`, `ACTION_SCHEMA`, and `_command_for`).
The system prompt in `agent.py` documents the action vocabulary for the model.
