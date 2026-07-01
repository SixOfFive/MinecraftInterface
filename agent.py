"""The LLM 'brain': turn observations into one Minecraft action per step.

The agent is idle (no LLM calls) until it has a goal/job or an unread chat.
Two kinds of directive:
  - goal  : a one-off task; completing it (goal_complete) or exhausting the step
            budget ends it.
  - job   : a standing directive (guard/patrol/follow...) that never times out
            and ignores spurious goal_complete — set it and walk away.

Anti-loop guards watch for repeated/ineffective actions and (a) inject a
corrective hint into the prompt, then (b) hard-stop and pause if it stays stuck,
so a confused model can never burn cycles hammering the same failing action.
"""

from __future__ import annotations

import asyncio
import json
from collections import deque
from typing import Optional

from bridge import BotBridge, BotError
from ollama_client import OllamaClient, OllamaError

ACTION_NAMES = [
    "none", "chat", "goto", "gotoPlayer", "follow", "stop", "lookAt",
    "mine", "place", "collect", "attack", "equip", "drop",
    "eat", "flee", "sleep", "craft", "depositChest", "withdrawChest",
]

# Flat schema — small local models handle it far better than nested args.
ACTION_SCHEMA = {
    "type": "object",
    "properties": {
        "thought": {"type": "string"},
        "say": {"type": "string"},
        "action": {"type": "string", "enum": ACTION_NAMES},
        "x": {"type": "number"},
        "y": {"type": "number"},
        "z": {"type": "number"},
        "name": {"type": "string"},       # block or item name
        "username": {"type": "string"},   # gotoPlayer / follow
        "target": {"type": "string"},     # attack/flee: username, mob name, or "hostile"
        "message": {"type": "string"},    # chat
        "range": {"type": "integer"},
        "count": {"type": "integer"},
        "maxDistance": {"type": "integer"},
        "dest": {"type": "string", "enum": ["hand", "off-hand", "head", "torso", "legs", "feet"]},
        "goal_complete": {"type": "boolean"},
    },
    "required": ["thought", "action", "goal_complete"],
}

SYSTEM_PROMPT = """You are the mind of a Minecraft character (Java Edition) named {username}. \
You perceive the world through a JSON observation each turn and act by choosing exactly ONE action. \
The game engine handles walking, pathfinding, and digging for you — you only decide WHAT to do next.

Your job: pursue the CURRENT GOAL when one is set, and chat naturally with players who talk to you. \
Take ONE concrete step per turn; you will be called again to continue. Never repeat an action that already succeeded or already failed the same way.

Coordinates are block positions. x = east(+)/west(-), z = south(+)/north(-), y = up(+)/down(-). \
Only use coordinates, players, blocks, and entities that appear in the observation — never invent them.

AUTOPILOT: the observation has an "autopilot" object. Survival is automatic — the bot auto-eats when hungry \
and auto-fights or flees from hostile mobs on its own. If autopilot.fighting or autopilot.fleeing is true, you do \
NOT need to handle combat; you may choose action "none" and let it, or keep working on your goal.

ACTIONS (set "action" plus the fields it needs):
- none — do nothing (use when talking only, waiting, observing, or while autopilot handles a threat).
- chat — say something in public chat. Put text in "message" (or just set "say").
- goto — walk to a coordinate. Fields: x, y, z, optional range.
- gotoPlayer — walk to a player. Fields: username.
- follow — continuously follow a player until you stop. Fields: username.
- stop — stop moving / following / fighting / digging.
- lookAt — look toward a coordinate. Fields: x, y, z.
- mine — find and dig the nearest block(s) of a type. Fields: name (e.g. "oak_log"), optional count.
- place — place a block from inventory at an EMPTY (air) coordinate. Fields: name, x, y, z.
- collect — walk over nearby dropped items to pick them up.
- attack — attack a target. Field: target = a player username, a mob name, or "hostile".
- equip — equip an item. Fields: name, optional dest.
- drop — drop items. Fields: name, optional count.
- eat — eat food to restore hunger. Optional field: name (else best food).
- flee — run away from danger. Optional field: target.
- sleep — sleep in a nearby bed (only works at night).
- craft — craft an item. Fields: name, optional count. Needs the ingredients (and usually a crafting_table nearby).
- depositChest / withdrawChest — put/take items in the nearest chest. Fields: name, optional count.

RULES:
- If there is no goal and no one is talking to you, choose action "none".
- To gather a resource, use "mine" on the matching block in nearbyBlocks, then "collect" the drops. Don't just follow a player when the goal needs an action.
- If an action FAILED (e.g. placed:false, arrived:false, mined:0, or an ANTI-LOOP warning appears), do something DIFFERENT next — a new action, new coordinates, or ask the player. Do not repeat a failing action.
- ALWAYS include exactly these fields: "thought" (brief reasoning), "action" (one name above), and "goal_complete" (true/false), plus only the extra fields the chosen action needs.
- Output a RAW JSON object only — no markdown, no code fences, no text before or after it.

Example (walk to a player and greet them):
{"thought":"HVR asked me to come, I'll path over","say":"On my way!","action":"gotoPlayer","username":"HVR","goal_complete":false}
"""


def build_system_prompt(username: str) -> str:
    # .replace (not .format) so the literal { } in the JSON example above are safe.
    return SYSTEM_PROMPT.replace("{username}", username)


class Agent:
    def __init__(
        self,
        bridge: BotBridge,
        llm: OllamaClient,
        username: str = "ClaudeBot",
        tick: float = 2.0,
        heartbeat: float = 0.0,
        max_goal_steps: int = 40,
        verbose: bool = True,
    ) -> None:
        self.bridge = bridge
        self.llm = llm
        self.username = username
        self.tick = tick
        self.heartbeat = heartbeat  # >0: re-issue last directive after this many idle seconds
        self.max_goal_steps = max_goal_steps
        self.verbose = verbose

        self.goal: Optional[str] = None
        self.persistent = False  # True => standing "job" that never times out
        self.last_directive: Optional[tuple[str, bool]] = None  # (text, is_job) for heartbeat resume
        self._goal_steps = 0
        self.history: deque[str] = deque(maxlen=8)
        self.recent: deque[tuple[str, bool, str]] = deque(maxlen=6)  # (sig, effective, summary)
        self.recent_chat: deque[str] = deque(maxlen=6)
        self._wake = asyncio.Event()
        self._running = False

    # -- external signals -------------------------------------------------
    def set_goal(self, goal: Optional[str]) -> None:
        self.goal = goal.strip() if goal else None
        self.persistent = False
        self._goal_steps = 0
        self.recent.clear()
        # An explicit clear (stop) also forgets the heartbeat target, so it stays stopped.
        self.last_directive = (self.goal, False) if self.goal else None
        self._wake.set()

    def set_job(self, job: Optional[str]) -> None:
        """A standing job: never times out, ignores spurious goal_complete."""
        self.goal = job.strip() if job else None
        self.persistent = bool(self.goal)
        self._goal_steps = 0
        self.recent.clear()
        self.last_directive = (self.goal, True) if self.goal else None
        self._wake.set()

    def note_chat(self, username: str, message: str) -> None:
        self.recent_chat.append(f"{username}: {message}")
        self._wake.set()

    # -- action dispatch --------------------------------------------------
    _TIMEOUTS = {"goto": 120, "gotoPlayer": 120, "follow": 15, "mine": 180, "place": 60,
                 "collect": 90, "lookAt": 15, "sleep": 60, "craft": 60,
                 "depositChest": 60, "withdrawChest": 60, "flee": 15}

    def _command_for(self, d: dict) -> Optional[tuple[str, dict]]:
        action = d.get("action", "none")
        if action in ("none", ""):
            return None
        if action == "chat":
            msg = d.get("message") or d.get("say") or ""
            return ("chat", {"message": msg}) if msg.strip() else None
        if action == "goto":
            return ("goto", _pick(d, "x", "y", "z", "range"))
        if action == "gotoPlayer":
            return ("gotoPlayer", _pick(d, "username", "range"))
        if action == "follow":
            return ("follow", _pick(d, "username", "range"))
        if action == "stop":
            return ("stop", {})
        if action == "lookAt":
            return ("lookAt", _pick(d, "x", "y", "z"))
        if action == "mine":
            return ("mine", _pick(d, "name", "count", "maxDistance"))
        if action == "place":
            return ("place", _pick(d, "name", "x", "y", "z"))
        if action == "collect":
            return ("collect", _pick(d, "maxDistance"))
        if action == "attack":
            return ("attack", _pick(d, "target"))
        if action == "equip":
            return ("equip", _pick(d, "name", "dest"))
        if action == "drop":
            return ("drop", _pick(d, "name", "count"))
        if action == "eat":
            return ("eat", _pick(d, "name"))
        if action == "flee":
            return ("flee", _pick(d, "target"))
        if action == "sleep":
            return ("sleep", {})
        if action == "craft":
            return ("craft", _pick(d, "name", "count"))
        if action == "depositChest":
            return ("depositChest", _pick(d, "name", "count"))
        if action == "withdrawChest":
            return ("withdrawChest", _pick(d, "name", "count"))
        return None

    # -- anti-loop --------------------------------------------------------
    @staticmethod
    def _sig(action: str, args: dict) -> str:
        return action if not args else action + ":" + json.dumps(args, sort_keys=True, separators=(",", ":"))

    @staticmethod
    def _effective(action: str, result, summary: str) -> bool:
        if action in ("none", ""):
            return True
        if isinstance(summary, str) and summary.startswith("error"):
            return False
        if not isinstance(result, dict):
            return True
        if result.get("error"):
            return False
        if action == "place":
            return result.get("placed") is not False
        if action in ("goto", "gotoPlayer"):
            return result.get("arrived") is not False
        if action == "mine":
            return (result.get("mined") or 0) > 0
        if action == "attack":
            return result.get("engaged") is not False
        if action == "collect":
            return (result.get("walkedTo") or 0) > 0
        if action == "chat":
            return result.get("sent") is not False
        if action == "eat":
            return result.get("ate") is not False
        if action == "craft":
            return result.get("crafted") is not False
        if action in ("depositChest", "withdrawChest"):
            return result.get("ok", True) is not False
        return True

    def _anti_loop_hint(self) -> tuple[str, bool]:
        if not self.recent:
            return "", False
        streak = 0
        for _sig, eff, _sum in reversed(self.recent):
            if eff:
                break
            streak += 1
        last_sig, _last_eff, last_sum = self.recent[-1]
        same = sum(1 for s, e, _ in self.recent if s == last_sig and not e)
        hint = ""
        if same >= 2:
            hint = (f"ANTI-LOOP: you already tried `{last_sig}` and it FAILED ({last_sum}) {same}x. "
                    "Do NOT repeat it — pick a DIFFERENT action, or different coordinates/target, "
                    "or ask the player in chat what to do.")
        elif streak >= 3:
            hint = (f"ANTI-LOOP: your last {streak} actions did nothing useful. Change strategy completely, "
                    "ask the player in chat, or if the goal is impossible set goal_complete=true.")
        hard_stop = streak >= 5 or same >= 4
        return hint, hard_stop

    # -- one think-act step ----------------------------------------------
    async def step(self) -> None:
        hint, hard_stop = self._anti_loop_hint()
        if hard_stop:
            self._log(f"STUCK — {'resetting job' if self.persistent else 'pausing goal'}: {self.goal}")
            try:
                await self.bridge.send("chat", message=f"I'm stuck ({_short(self.goal, 60)}); taking a breather — tell me what to do.", timeout=15)
            except BotError:
                pass
            await self._halt()
            self.recent.clear()
            self._goal_steps = 0
            if not self.persistent:
                self.goal = None
            return

        try:
            state = await self.bridge.send("state", timeout=15)
        except BotError as e:
            self._log(f"(state unavailable: {e})")
            return

        messages = [
            {"role": "system", "content": build_system_prompt(self.username)},
            {"role": "user", "content": self._observation(state, hint)},
        ]
        try:
            decision = await self.llm.chat(messages, schema=ACTION_SCHEMA)
        except OllamaError as e:
            self._log(f"(LLM error: {e})")
            return

        thought = str(decision.get("thought", "")).strip()
        say = str(decision.get("say", "")).strip()
        action = decision.get("action", "none")
        self._log(f"think: {thought}  -> {action}")

        if say and action != "chat":
            try:
                await self.bridge.send("chat", message=say, timeout=15)
            except BotError:
                pass

        cmd = self._command_for(decision)
        result_obj = None
        result_summary = "none"
        if cmd is not None:
            name, args = cmd
            try:
                result_obj = await self.bridge.send(name, timeout=self._TIMEOUTS.get(name, 60), **args)
                result_summary = _short(result_obj)
            except BotError as e:
                result_summary = f"error: {e}"
            self._log(f"do: {name}({_short(args)}) -> {result_summary}")

        effective = self._effective(action, result_obj, result_summary)
        self.recent.append((self._sig(action, cmd[1] if cmd else {}), effective, result_summary))
        self.history.append(f"{action}({_short(cmd[1]) if cmd else ''}) -> {result_summary}")

        if bool(decision.get("goal_complete")) and self.goal is not None and not self.persistent:
            self._log(f"goal complete: {self.goal}")
            self.goal = None
            self._goal_steps = 0
            await self._halt()

    # -- main loop --------------------------------------------------------
    async def run(self) -> None:
        self._running = True
        while self._running:
            if self.goal is None:
                if self.heartbeat > 0 and self.last_directive is not None:
                    # Idle watchdog: wait for a wake, but if none comes within the
                    # heartbeat window, re-issue the last directive to resume.
                    try:
                        await asyncio.wait_for(self._wake.wait(), timeout=self.heartbeat)
                        self._wake.clear()
                    except asyncio.TimeoutError:
                        if self.goal is None and self.last_directive is not None:
                            text, is_job = self.last_directive
                            self._log(f"heartbeat: resuming {'job' if is_job else 'goal'}: {text!r}")
                            self.set_job(text) if is_job else self.set_goal(text)
                        continue
                else:
                    await self._wake.wait()
                    self._wake.clear()
                if not self._running:
                    break
            else:
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=self.tick)
                    self._wake.clear()
                except asyncio.TimeoutError:
                    pass

            if not self.bridge.ready:
                await asyncio.sleep(0.5)
                continue

            self._goal_steps += 1
            await self.step()

            if self.goal is not None and self._goal_steps >= self.max_goal_steps:
                if self.persistent:
                    self._goal_steps = 0  # standing job: reset budget, keep going
                else:
                    self._log(f"giving up on goal after {self.max_goal_steps} steps: {self.goal}")
                    self.goal = None
                    self._goal_steps = 0
                    await self._halt()

    async def _halt(self) -> None:
        try:
            await self.bridge.send("stop", timeout=15)
        except BotError:
            pass

    def stop(self) -> None:
        self._running = False
        self._wake.set()

    # -- prompt building --------------------------------------------------
    def _observation(self, state: dict, hint: str = "") -> str:
        chat_block = "\n".join(self.recent_chat) if self.recent_chat else "(none)"
        hist_block = "\n".join(self.history) if self.history else "(none)"
        kind = "JOB (standing, never auto-completes)" if self.persistent else "GOAL"
        goal = self.goal if self.goal else "(none — idle; only chat or observe)"
        hint_block = f"{hint}\n\n" if hint else ""
        return (
            f"{hint_block}"
            f"CURRENT {kind}: {goal}\n\n"
            f"RECENT CHAT:\n{chat_block}\n\n"
            f"RECENT ACTIONS (oldest first):\n{hist_block}\n\n"
            f"OBSERVATION:\n{json.dumps(state, separators=(',', ':'))}\n\n"
            "Choose your next action as JSON."
        )

    def _log(self, text: str) -> None:
        if self.verbose:
            print(f"[agent] {text}", flush=True)


def _pick(d: dict, *keys: str) -> dict:
    return {k: d[k] for k in keys if k in d and d[k] is not None}


def _short(obj, limit: int = 160) -> str:
    try:
        s = obj if isinstance(obj, str) else json.dumps(obj, separators=(",", ":"))
    except (TypeError, ValueError):
        s = str(obj)
    return s if len(s) <= limit else s[: limit - 1] + "…"
