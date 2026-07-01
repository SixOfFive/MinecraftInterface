'use strict'
/*
 * bot.js — thin Mineflayer "hands" for an LLM brain, plus a fast reflex layer.
 *
 * Two layers:
 *   1. ACTIONS — one low-level action per request from the Python controller
 *      (all high-level decisions live in Python).
 *   2. REFLEXES — a ~2Hz loop that runs independently of the (slow) LLM so the
 *      bot survives unattended: auto-eat, auto-defend, flee at low health,
 *      auto-pickup, and idle life (wander/greet).
 *
 * Protocol (one JSON object per line, both directions):
 *   command  (Python -> bot):  {"id": 7, "cmd": "goto", "args": {"x":10,"y":64,"z":-3}}
 *   response (bot -> Python):  {"id": 7, "ok": true,  "result": {...}}
 *                              {"id": 7, "ok": false, "error": "..."}
 *   event    (bot -> Python):  {"event": "chat", "data": {...}}  /  {"event":"auto","data":{"kind":"defend",...}}
 *
 * Human logging -> stderr (console.error). Protocol -> the TCP socket. stdout is unused.
 */

const net = require('net')
const mineflayer = require('mineflayer')
const { pathfinder, Movements, goals } = require('mineflayer-pathfinder')
const { GoalNear, GoalFollow, GoalGetToBlock } = goals
const { Vec3 } = require('vec3')

let pvpPlugin = null
let collectPlugin = null
try { pvpPlugin = require('mineflayer-pvp').plugin } catch (e) { log('mineflayer-pvp not installed; using fallback combat') }
try { collectPlugin = require('mineflayer-collectblock').plugin } catch (e) { log('mineflayer-collectblock not installed; using fallback mining') }

process.on('unhandledRejection', (e) => log('unhandledRejection:', (e && e.message) || e))

function envBool (k, d) { const v = process.env[k]; if (v == null) return d; return v !== 'false' && v !== '0' }

const CONFIG = {
  mcHost: process.env.MC_HOST || '127.0.0.1',
  mcPort: parseInt(process.env.MC_PORT || '25565', 10),
  username: process.env.MC_USERNAME || 'ClaudeBot',
  auth: process.env.MC_AUTH || 'offline',
  version: (process.env.MC_VERSION && process.env.MC_VERSION.length) ? process.env.MC_VERSION : false,
  bridgeHost: process.env.BRIDGE_HOST || '127.0.0.1',
  bridgePort: parseInt(process.env.BRIDGE_PORT || '25585', 10),
  // Headless bots don't need to see far; a small view distance keeps chunk memory
  // low and prevents the Node heap from ballooning (OOM). tiny|short|normal|far.
  viewDistance: process.env.MC_VIEW_DISTANCE || 'tiny',
  // canDig true makes the pathfinder treat every block as diggable, exploding the A*
  // search (millions of nodes -> GBs -> heap OOM) when a target is buried. Off by default.
  canDig: envBool('MOVE_CAN_DIG', false),
  autoReconnect: envBool('MC_AUTO_RECONNECT', true),
  reconnectDelayMs: parseInt(process.env.MC_RECONNECT_MS || '5000', 10),
  maxReconnect: parseInt(process.env.MC_MAX_RECONNECT || '5', 10), // 0 = retry forever
}

// Reflex configuration (mutable at runtime via the setReflexes command).
const RX = {
  autoEat: envBool('AUTO_EAT', true),
  eatAt: parseFloat(process.env.EAT_AT || '16'),
  autoDefend: envBool('AUTO_DEFEND', true),
  defendRadius: parseFloat(process.env.DEFEND_RADIUS || '6'),
  fleeHealth: parseFloat(process.env.FLEE_HEALTH || '6'),
  autoPickup: envBool('AUTO_PICKUP', true),
  pickupRadius: parseFloat(process.env.PICKUP_RADIUS || '6'),
  idleWander: envBool('IDLE_WANDER', true),
  wanderRadius: parseFloat(process.env.WANDER_RADIUS || '5'),
  wanderInterval: parseInt(process.env.WANDER_INTERVAL || '30000', 10),
  greet: envBool('GREET', true),
  interval: parseInt(process.env.REFLEX_INTERVAL || '500', 10),
}
let OWNER = process.env.MC_OWNER || '' // player the bot protects / flees toward

const INTEREST_NAMES = [
  'oak_log', 'birch_log', 'spruce_log', 'jungle_log', 'acacia_log', 'dark_oak_log', 'mangrove_log', 'cherry_log',
  'coal_ore', 'iron_ore', 'copper_ore', 'gold_ore', 'diamond_ore', 'redstone_ore', 'lapis_ore', 'emerald_ore',
  'deepslate_coal_ore', 'deepslate_iron_ore', 'deepslate_copper_ore', 'deepslate_gold_ore', 'deepslate_diamond_ore',
  'deepslate_redstone_ore', 'deepslate_lapis_ore', 'ancient_debris',
  'water', 'lava', 'crafting_table', 'furnace', 'chest', 'ender_chest', 'bed',
  'dirt', 'grass_block', 'sand', 'gravel', 'stone', 'cobblestone', 'obsidian', 'wheat',
]

// Blocks harvestNearest will auto-gather (best/nearest of any of these).
const RESOURCE_NAMES = [
  'oak_log', 'birch_log', 'spruce_log', 'jungle_log', 'acacia_log', 'dark_oak_log', 'mangrove_log', 'cherry_log',
  'coal_ore', 'iron_ore', 'copper_ore', 'gold_ore', 'diamond_ore', 'redstone_ore', 'lapis_ore', 'emerald_ore',
  'deepslate_coal_ore', 'deepslate_iron_ore', 'deepslate_copper_ore', 'deepslate_gold_ore', 'deepslate_diamond_ore',
  'deepslate_redstone_ore', 'deepslate_lapis_ore', 'ancient_debris',
  'stone', 'cobblestone', 'sand', 'gravel', 'pumpkin', 'melon',
]

const HOSTILE_NAMES = new Set([
  'zombie', 'husk', 'drowned', 'skeleton', 'stray', 'bogged', 'creeper', 'spider', 'cave_spider',
  'witch', 'enderman', 'slime', 'silverfish', 'zombified_piglin', 'piglin', 'piglin_brute',
  'hoglin', 'zoglin', 'blaze', 'ghast', 'magma_cube', 'phantom', 'pillager', 'vindicator',
  'evoker', 'ravager', 'vex', 'shulker', 'guardian', 'elder_guardian', 'warden', 'wither_skeleton', 'wither',
])

const BAD_FOODS = new Set(['rotten_flesh', 'spider_eye', 'poisonous_potato', 'pufferfish', 'chorus_fruit', 'suspicious_stew'])

const GREETINGS = ['hey', 'hello', 'hi there', 'oh hi', 'hey there']

// ---------------------------------------------------------------------------
// Bridge (NDJSON over TCP, one controller at a time)
// ---------------------------------------------------------------------------
let client = null
function send (obj) {
  if (client && !client.destroyed) { try { client.write(JSON.stringify(obj) + '\n') } catch (e) {} }
}
function emit (event, data) { send({ event, data: data || {} }) }
function log (...args) { console.error('[bot]', ...args) }

const server = net.createServer((sock) => {
  if (client && !client.destroyed) { try { client.destroy() } catch (e) {} }
  client = sock
  sock.setEncoding('utf8')
  let buf = ''
  log('controller connected')
  emit('bridge_connected', { ready: botReady })
  sock.on('data', (chunk) => {
    buf += chunk
    let idx
    while ((idx = buf.indexOf('\n')) >= 0) {
      const line = buf.slice(0, idx)
      buf = buf.slice(idx + 1)
      if (line.trim()) handleLine(line)
    }
  })
  sock.on('error', () => {})
  sock.on('close', () => { if (client === sock) { client = null; log('controller disconnected') } })
})
server.on('error', (e) => { log('bridge server error:', e.message); process.exit(1) })
server.listen(CONFIG.bridgePort, CONFIG.bridgeHost, () => {
  log(`control bridge listening on ${CONFIG.bridgeHost}:${CONFIG.bridgePort}`)
})

async function handleLine (line) {
  let msg
  try { msg = JSON.parse(line) } catch (e) { return }
  const { id, cmd } = msg
  const args = msg.args || {}
  const handler = ACTIONS[cmd]
  if (!handler) { send({ id, ok: false, error: `unknown command: ${cmd}` }); return }
  try {
    const result = await handler(args)
    send({ id, ok: true, result: result === undefined ? null : result })
  } catch (e) {
    send({ id, ok: false, error: (e && e.message) ? e.message : String(e) })
  }
}

// ---------------------------------------------------------------------------
// Bot lifecycle
// ---------------------------------------------------------------------------
let bot = null
let botReady = false
let mcData = null
let interestIds = []
let resourceIds = []
let shuttingDown = false
let reconnectAttempts = 0
// reflex state
let reflexTimer = null
let reflexRunning = false
let fleeing = false
let lastWander = 0
let lastDefend = 0
let lastReflexErr = 0
let greetedAt = {}

function createBot () {
  botReady = false
  try { if (bot) bot.removeAllListeners() } catch (e) {}
  stopReflexes()
  fleeing = false
  greetedAt = {}
  log(`connecting to ${CONFIG.mcHost}:${CONFIG.mcPort} as "${CONFIG.username}" (auth=${CONFIG.auth}, version=${CONFIG.version || 'auto'})`)
  bot = mineflayer.createBot({
    host: CONFIG.mcHost, port: CONFIG.mcPort, username: CONFIG.username,
    auth: CONFIG.auth, version: CONFIG.version,
    viewDistance: CONFIG.viewDistance, // keep chunk memory small (avoids heap OOM)
  })
  bot.loadPlugin(pathfinder)
  if (pvpPlugin) bot.loadPlugin(pvpPlugin)
  if (collectPlugin) bot.loadPlugin(collectPlugin)

  bot.once('spawn', () => {
    botReady = true
    reconnectAttempts = 0 // connected successfully — reset the give-up counter
    mcData = bot.registry
    interestIds = INTEREST_NAMES
      .map((n) => mcData.blocksByName[n] && mcData.blocksByName[n].id)
      .filter((v) => v !== undefined && v !== null)
    resourceIds = RESOURCE_NAMES
      .map((n) => mcData.blocksByName[n] && mcData.blocksByName[n].id)
      .filter((v) => v !== undefined && v !== null)
    const move = new Movements(bot)
    move.canDig = CONFIG.canDig
    move.allow1by1towers = false // pillaring also balloons the pathfinder search/memory
    move.allowSprinting = true
    move.allowParkour = true
    bot.pathfinder.setMovements(move)
    // Cap A* time so a single hard/unreachable search can't allocate unbounded memory.
    bot.pathfinder.thinkTimeout = parseInt(process.env.PATHFINDER_TIMEOUT_MS || '4000', 10)
    log(`spawned as ${bot.username} (v${bot.version}) at`, prettyVec(bot.entity.position))
    emit('spawn', {
      username: bot.username, version: bot.version, position: vec(bot.entity.position),
      capabilities: { pvp: !!bot.pvp, collect: !!bot.collectBlock },
    })
    startReflexes()
  })

  bot.on('chat', (username, message) => { if (username !== bot.username) emit('chat', { username, message }) })
  bot.on('playerCollect', (collector, collected) => { if (collector === bot.entity) emit('collect', { item: collected && collected.name }) })
  bot.on('death', () => { log('died'); emit('death', {}) })
  bot.on('kicked', (reason) => { log('kicked:', reason); emit('kicked', { reason: String(reason) }) })
  bot.on('error', (err) => { log('error:', err.message); emit('error', { message: err.message }) })
  bot.on('end', (reason) => {
    botReady = false
    stopReflexes()
    log('disconnected:', reason)
    emit('end', { reason: String(reason) })
    if (shuttingDown) return
    if (!CONFIG.autoReconnect) {
      log('auto-reconnect disabled — exiting so the controller can shut down.')
      try { server.close() } catch (e) {}
      process.exit(1)
    }
    reconnectAttempts++
    if (CONFIG.maxReconnect > 0 && reconnectAttempts > CONFIG.maxReconnect) {
      log(`gave up after ${CONFIG.maxReconnect} failed reconnect attempts (world/server gone?) — exiting.`)
      try { server.close() } catch (e) {}
      process.exit(1)
    }
    log(`reconnecting in ${CONFIG.reconnectDelayMs}ms (attempt ${reconnectAttempts}${CONFIG.maxReconnect > 0 ? '/' + CONFIG.maxReconnect : ''}) ...`)
    setTimeout(createBot, CONFIG.reconnectDelayMs)
  })
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------
function sleep (ms) { return new Promise((r) => setTimeout(r, ms)) }
function round (n) { return Math.round(n * 100) / 100 }
function vec (p) { return p ? { x: round(p.x), y: round(p.y), z: round(p.z) } : null }
function prettyVec (p) { return p ? `(${Math.round(p.x)}, ${Math.round(p.y)}, ${Math.round(p.z)})` : '(?)' }
function deg2rad (d) { return d * Math.PI / 180 }
function rad2deg (r) { return r * 180 / Math.PI }
function clamp (n, lo, hi) { return Math.max(lo, Math.min(hi, n)) }
function num (v, label) { const n = Number(v); if (!Number.isFinite(n)) throw new Error(`missing or invalid ${label || 'number'}`); return n }
function requireBot () { if (!bot || !botReady) throw new Error('bot not ready (not spawned yet)') }
function stopCombat () { try { if (bot.pvp && bot.pvp.target) bot.pvp.stop() } catch (e) {} }
function isBusy () { try { return bot.pathfinder.isMoving() || bot.pathfinder.isMining() || (bot.pvp && bot.pvp.target) } catch (e) { return false } }

function describeEntity (e) {
  if (!e) return null
  return { name: e.name || e.username || e.displayName || 'entity', type: e.type, position: vec(e.position) }
}
function resolveTarget (spec) {
  if (spec && bot.players[spec] && bot.players[spec].entity) return bot.players[spec].entity
  if (!spec || spec === 'hostile' || spec === 'nearest' || spec === 'mob') {
    return bot.nearestEntity((e) => e.type === 'hostile' || (e.name && HOSTILE_NAMES.has(e.name)))
  }
  return bot.nearestEntity((e) => e.name === spec || e.displayName === spec)
}
function nearestThreat (radius) {
  try {
    return bot.nearestEntity((e) =>
      (e.type === 'hostile' || (e.name && HOSTILE_NAMES.has(e.name))) &&
      e.position && bot.entity.position.distanceTo(e.position) <= radius)
  } catch (e) { return null }
}
function bestFood () {
  let best = null, bestFp = -1
  for (const it of bot.inventory.items()) {
    const fd = mcData.foodsByName && mcData.foodsByName[it.name]
    if (!fd || BAD_FOODS.has(it.name)) continue
    const fp = (fd.food_points != null ? fd.food_points : fd.foodPoints) || 0
    if (fp > bestFp) { bestFp = fp; best = it }
  }
  return best
}
// Items stashResources should NOT deposit (tools, weapons, armor, food, essentials).
function isKeepItem (name) {
  if (mcData.foodsByName && mcData.foodsByName[name]) return true
  return /pickaxe|axe|shovel|sword|hoe|bow|crossbow|shield|helmet|chestplate|leggings|boots|elytra|totem|torch|bucket|flint_and_steel|shears|ender_pearl|_bed$/.test(name)
}

// ---------------------------------------------------------------------------
// Crafting / gear helpers (all fail soft — never throw out of gearUp/craft).
// ---------------------------------------------------------------------------
const TOOL_SPECS = {
  wooden_pickaxe: { base: 'planks', baseCount: 3, sticks: 2 },
  wooden_sword: { base: 'planks', baseCount: 2, sticks: 1 },
  wooden_axe: { base: 'planks', baseCount: 3, sticks: 2 },
  stone_pickaxe: { base: 'cobblestone', baseCount: 3, sticks: 2 },
  stone_sword: { base: 'cobblestone', baseCount: 2, sticks: 1 },
  stone_axe: { base: 'cobblestone', baseCount: 3, sticks: 2 },
  iron_pickaxe: { base: 'iron_ingot', baseCount: 3, sticks: 2 },
  iron_sword: { base: 'iron_ingot', baseCount: 2, sticks: 1 },
  iron_axe: { base: 'iron_ingot', baseCount: 3, sticks: 2 },
}
// Iron armor — crafted from iron_ingot on a table, then auto-equipped. Ordered
// by protection so a bot short on iron still gets the most-valuable piece first.
const ARMOR_SPECS = {
  iron_chestplate: { iron: 8, slot: 'torso', key: 'torso' },
  iron_leggings: { iron: 7, slot: 'legs', key: 'legs' },
  iron_helmet: { iron: 5, slot: 'head', key: 'head' },
  iron_boots: { iron: 4, slot: 'feet', key: 'feet' },
}
const ARMOR_PRIORITY = ['iron_chestplate', 'iron_leggings', 'iron_helmet', 'iron_boots']
const TOOL_KINDS = ['netherite', 'diamond', 'iron', 'stone', 'golden', 'wooden']

function invCount (name) { return bot.inventory.items().filter((i) => i.name === name).reduce((a, i) => a + i.count, 0) }
function invCountRe (re) { return bot.inventory.items().filter((i) => re.test(i.name)).reduce((a, i) => a + i.count, 0) }
function firstItem (re) { return bot.inventory.items().find((i) => re.test(i.name)) }
function logToPlanks (logName) {
  const plank = logName.replace(/_log$|_wood$|_stem$|_hyphae$/, '') + '_planks'
  return mcData.itemsByName[plank] ? plank : 'oak_planks'
}
function nearbyTable () {
  const def = mcData.blocksByName.crafting_table
  return def ? bot.findBlock({ matching: def.id, maxDistance: 4 }) : null
}
async function craftOne (itemName, table) {
  const def = mcData.itemsByName[itemName]
  if (!def) return false
  const recipes = bot.recipesFor(def.id, null, 1, table || null)
  if (!recipes.length) return false
  try { await bot.craft(recipes[0], 1, table || undefined); return true } catch (e) { return false }
}
async function ensurePlanks (need) {
  let guard = 0
  while (invCountRe(/_planks$/) < need && guard++ < 20) {
    const log = firstItem(/_log$|_wood$|_stem$|_hyphae$/)
    if (!log || !await craftOne(logToPlanks(log.name), null)) return false
  }
  return invCountRe(/_planks$/) >= need
}
async function ensureSticks (need) {
  let guard = 0
  while (invCount('stick') < need && guard++ < 20) {
    if (!await ensurePlanks(2) || !await craftOne('stick', null)) return false
  }
  return invCount('stick') >= need
}
async function ensureTable () {
  let table = nearbyTable()
  if (table) return table
  let item = bot.inventory.items().find((i) => i.name === 'crafting_table')
  if (!item) {
    if (!await ensurePlanks(4) || !await craftOne('crafting_table', null)) return null
    item = bot.inventory.items().find((i) => i.name === 'crafting_table')
    if (!item) return null
  }
  try {
    await bot.equip(item, 'hand')
    const base = bot.entity.position.floored()
    for (const [dx, dz] of [[1, 0], [-1, 0], [0, 1], [0, -1]]) {
      const ref = bot.blockAt(base.offset(dx, -1, dz))
      const cell = bot.blockAt(base.offset(dx, 0, dz))
      if (ref && ref.boundingBox === 'block' && cell && cell.name === 'air') {
        try { await bot.lookAt(cell.position.offset(0.5, 0.5, 0.5)); await bot.placeBlock(ref, new Vec3(0, 1, 0)); break } catch (e) {}
      }
    }
  } catch (e) {}
  return nearbyTable()
}
function toolTier (kind) {
  for (const t of TOOL_KINDS) if (bot.inventory.items().find((i) => i.name === `${t}_${kind}`)) return t
  return null
}
function gearSummary () {
  return {
    pickaxe: toolTier('pickaxe'), sword: toolTier('sword'), axe: toolTier('axe'), shovel: toolTier('shovel'),
    logs: invCountRe(/_log$/), planks: invCountRe(/_planks$/), sticks: invCount('stick'), cobblestone: invCount('cobblestone'),
    iron: invCount('iron_ingot'), rawIron: invCount('raw_iron') + invCount('iron_ore'),
    coal: invCount('coal') + invCount('charcoal'),
    furnace: !!nearbyFurnace() || invCount('furnace') > 0,
    armor: { head: haveIronPlus('helmet'), torso: haveIronPlus('chestplate'), legs: haveIronPlus('leggings'), feet: haveIronPlus('boots') },
  }
}
async function makeTool (name) {
  const spec = TOOL_SPECS[name]
  if (!spec) return { ok: false, reason: `don't know how to make ${name}` }
  if (spec.base === 'planks') {
    if (!await ensurePlanks(spec.baseCount)) return { ok: false, need: 'oak_log', reason: 'need wood — harvest logs' }
  } else if (spec.base === 'iron_ingot') {
    if (invCount('iron_ingot') < spec.baseCount) return { ok: false, need: 'raw_iron', reason: 'need iron ingots — smelt raw_iron in a furnace (gearUp)' }
  } else if (invCount('cobblestone') < spec.baseCount) {
    return { ok: false, need: 'stone', reason: 'need cobblestone — mine stone (harvestNearest)' }
  }
  if (!await ensureSticks(spec.sticks)) return { ok: false, need: 'oak_log', reason: 'need wood for sticks' }
  const table = await ensureTable()
  if (!table) return { ok: false, need: 'oak_log', reason: 'need a crafting table (and wood to make it)' }
  return await craftOne(name, table) ? { ok: true, crafted: name } : { ok: false, reason: `could not craft ${name}` }
}

// --- iron age: furnace, smelting, armor -----------------------------------
function nearbyFurnace () {
  return bot.findBlock({ matching: (b) => b && (b.name === 'furnace' || b.name === 'blast_furnace'), maxDistance: 6 })
}
// Fuel preference: coal-family first (each smelts 8 and isn't needed for crafting),
// then wood as a last resort. Note wood fuel competes with the ladder's need for
// sticks/planks, so gearUp reports need:'oak_log' if it later runs short.
function findFuelItem () {
  const prefs = [/^coal$/, /^charcoal$/, /^coal_block$/, /_log$/, /_wood$/, /_planks$/, /^stick$/]
  for (const re of prefs) { const it = bot.inventory.items().find((i) => re.test(i.name)); if (it) return it }
  return null
}
function emptySlotCount () {
  try { if (typeof bot.inventory.emptySlotCount === 'function') return bot.inventory.emptySlotCount() } catch (e) {}
  return (bot.inventory.slots || []).filter((s) => !s).length
}
// Best armor tier the bot has for a slot (0 = none). Scans inventory + worn armor
// slots. gearUp uses this so it crafts iron only when a slot is worse than iron —
// and never DOWNGRADES an existing diamond/netherite piece to iron.
const ARMOR_TIER = { leather: 1, golden: 2, chainmail: 2, iron: 3, diamond: 4, netherite: 5 }
function armorTierInSlot (kind) {
  let best = 0
  for (const s of (bot.inventory.slots || [])) {
    if (!s) continue
    const m = s.name.match(new RegExp(`^(\\w+?)_${kind}$`))
    if (m && ARMOR_TIER[m[1]] != null) best = Math.max(best, ARMOR_TIER[m[1]])
  }
  return best
}
function haveIronPlus (kind) { return armorTierInSlot(kind) >= ARMOR_TIER.iron }
// Find or build+place a furnace. Mirrors ensureTable's placement scan.
async function ensureFurnace () {
  let f = nearbyFurnace()
  if (f) return f
  let item = bot.inventory.items().find((i) => i.name === 'furnace')
  if (!item) {
    if (invCount('cobblestone') < 8) return null
    const table = await ensureTable()
    if (!table || !await craftOne('furnace', table)) return null
    item = bot.inventory.items().find((i) => i.name === 'furnace')
    if (!item) return null
  }
  try {
    await bot.equip(item, 'hand')
    const base = bot.entity.position.floored()
    for (const [dx, dz] of [[1, 0], [-1, 0], [0, 1], [0, -1], [1, 1], [-1, -1], [1, -1], [-1, 1]]) {
      const ref = bot.blockAt(base.offset(dx, -1, dz))
      const cell = bot.blockAt(base.offset(dx, 0, dz))
      if (ref && ref.boundingBox === 'block' && cell && cell.name === 'air') {
        try { await bot.lookAt(cell.position.offset(0.5, 0.5, 0.5)); await bot.placeBlock(ref, new Vec3(0, 1, 0)); break } catch (e) {}
      }
    }
  } catch (e) {}
  return nearbyFurnace()
}
// Smelt up to `maxItems` raw iron. Fail-soft: recovers input/output on timeout,
// always closes the furnace. Blocks for the smelt (reflexes run independently).
async function smeltIron (maxItems) {
  const rawName = invCount('raw_iron') > 0 ? 'raw_iron' : (invCount('iron_ore') > 0 ? 'iron_ore' : null)
  if (!rawName) return { ok: false, need: 'raw_iron', reason: 'no raw iron — mine iron_ore' }
  const fuel = findFuelItem()
  if (!fuel) return { ok: false, need: 'coal', reason: 'no fuel — get coal or wood' }
  if (emptySlotCount() < 2) return { ok: false, need: 'inventory_space', reason: 'inventory full — free a slot before smelting' }
  const fBlock = await ensureFurnace()
  if (!fBlock) {
    return invCount('furnace') > 0
      ? { ok: false, need: 'space', reason: 'have a furnace but no room to place it — move to open ground' }
      : { ok: false, need: 'cobblestone', reason: 'need a furnace (8 cobblestone)' }
  }
  try { await bot.pathfinder.goto(new GoalGetToBlock(fBlock.position.x, fBlock.position.y, fBlock.position.z)) } catch (e) {}
  let furnace
  try { furnace = await bot.openFurnace(fBlock) } catch (e) { return { ok: false, error: 'could not open furnace: ' + e.message } }
  const before = invCount('iron_ingot')
  try {
    const want = Math.min(invCount(rawName), Math.max(1, maxItems || 8))
    const rawDef = mcData.itemsByName[rawName]
    const fuelDef = mcData.itemsByName[fuel.name]
    const fuelPer = /coal|charcoal/.test(fuel.name) ? 8 : 1.5
    const loadFuel = Math.min(Math.max(1, Math.ceil(want / fuelPer)), fuel.count)
    try { await furnace.putFuel(fuelDef.id, null, loadFuel) } catch (e) {}
    // A furnace with no input won't burn yet, so a landed fuel item stays in its
    // slot — if it's absent here, the transfer failed. Bail before the long wait.
    if (!furnace.fuelItem()) {
      try { furnace.close() } catch (x) {}
      return { ok: false, need: 'coal', reason: 'furnace would not accept fuel' }
    }
    try { await furnace.putInput(rawDef.id, null, want) } catch (e) {
      try { furnace.close() } catch (x) {}
      return { ok: false, error: 'could not load furnace: ' + e.message }
    }
    // Wait only as long as the loaded fuel can actually smelt (bounds a starved
    // smelt), with a stall guard so a stuck furnace never pins the full timeout.
    const canSmelt = Math.min(want, Math.max(1, Math.floor(loadFuel * fuelPer)))
    const timeoutMs = Math.min(120000, canSmelt * 11000 + 12000)
    const start = Date.now()
    let lastOut = -1
    let stall = 0
    while (Date.now() - start < timeoutMs) {
      const out = furnace.outputItem()
      const c = out ? out.count : 0
      if (c >= canSmelt) break
      if (!furnace.inputItem()) break // input consumed
      if (c === lastOut) { if (++stall >= 15) break } else { stall = 0; lastOut = c }
      await sleep(1000)
    }
    try { if (furnace.outputItem()) await furnace.takeOutput() } catch (e) {}
    try { if (furnace.inputItem()) await furnace.takeInput() } catch (e) {} // recover un-smelted raw
  } catch (e) {
    try { furnace.close() } catch (x) {}
    return { ok: false, error: e.message }
  }
  try { furnace.close() } catch (e) {}
  const smelted = invCount('iron_ingot') - before
  if (smelted > 0) return { ok: true, smelted, iron: invCount('iron_ingot') }
  // Nothing reached the inventory — a full pack means the ingots are stranded in
  // the furnace (takeOutput failed), which is a distinct, actionable failure.
  return emptySlotCount() < 1
    ? { ok: false, need: 'inventory_space', reason: 'smelted iron but inventory full — free a slot to collect it', iron: invCount('iron_ingot') }
    : { ok: false, reason: 'smelting produced nothing (out of fuel?)', iron: invCount('iron_ingot') }
}
// Craft one armor piece and auto-equip it.
async function makeArmor (name) {
  const spec = ARMOR_SPECS[name]
  if (!spec) return { ok: false, reason: `don't know armor ${name}` }
  if (invCount('iron_ingot') < spec.iron) return { ok: false, need: 'raw_iron', reason: `need ${spec.iron} iron for ${name}` }
  const table = await ensureTable()
  if (!table) return { ok: false, need: 'oak_log', reason: 'need a crafting table' }
  if (!await craftOne(name, table)) return { ok: false, reason: `could not craft ${name}` }
  const item = bot.inventory.items().find((i) => i.name === name)
  if (item) { try { await bot.equip(item, spec.slot) } catch (e) {} }
  return { ok: true, crafted: name, equipped: !!item }
}
// When gearUp can't act, tell the caller what raw material to gather next.
function nextGearNeed (g) {
  const armorLeft = ['torso', 'legs', 'head', 'feet'].filter((k) => !g.armor[k])
  const toolsLeft = g.pickaxe === 'stone' || g.sword === 'stone'
  if (!toolsLeft && armorLeft.length === 0) return { done: true, message: 'fully geared (iron tools + armor)', gear: g }
  if (g.rawIron > 0 && !g.furnace && g.cobblestone < 8) return { done: false, need: 'cobblestone', message: 'have raw iron — mine 8 cobblestone to build a furnace', gear: g }
  if (g.rawIron > 0 && !findFuelItem()) return { done: false, need: 'coal', message: 'have raw iron — need fuel (coal or wood) to smelt', gear: g }
  if (g.iron === 0 && g.rawIron === 0) {
    if (!g.furnace && g.cobblestone < 8) return { done: false, need: 'cobblestone', message: 'to reach iron gear: mine cobblestone (furnace) then iron_ore', gear: g }
    return { done: false, need: 'raw_iron', message: 'mine iron_ore to smelt into iron gear', gear: g }
  }
  return { done: false, need: 'raw_iron', message: 'gather more iron_ore to finish gear', gear: g }
}

async function equipBestTool (block) {
  const n = block.name
  let kind = null
  if (/ore|stone|cobble|deepslate|granite|diorite|andesite|obsidian|furnace/.test(n)) kind = 'pickaxe'
  else if (/log|planks|wood|fence|crafting_table|bookshelf/.test(n)) kind = 'axe'
  else if (/dirt|grass|sand|gravel|clay|soul|snow|mud/.test(n)) kind = 'shovel'
  if (!kind) return
  const tool = bot.inventory.items().find((i) => i.name.includes(kind))
  if (tool) { try { await bot.equip(tool, 'hand') } catch (e) {} }
}
async function manualAttack (target, maxMs) {
  const deadline = Date.now() + maxMs
  bot.pathfinder.setGoal(new GoalFollow(target, 2), true)
  while (Date.now() < deadline) {
    if (!target.isValid) break
    if (bot.entity.position.distanceTo(target.position) <= 3.3) bot.attack(target)
    await sleep(600)
  }
  try { bot.pathfinder.setGoal(null) } catch (e) {}
}
async function openNearestChest () {
  const chestBlock = bot.findBlock({ matching: (b) => b && /(^|_)chest$/.test(b.name) && b.name !== 'ender_chest', maxDistance: 48 })
  if (!chestBlock) return null
  try { await bot.pathfinder.goto(new GoalGetToBlock(chestBlock.position.x, chestBlock.position.y, chestBlock.position.z)) } catch (e) {}
  try { return await bot.openContainer(chestBlock) } catch (e) { return null }
}

// ---------------------------------------------------------------------------
// Reflex layer — runs ~2Hz, independent of the LLM. Priority: flee > defend >
// eat > pickup > greet > wander. Everything is wrapped so it can never crash.
// ---------------------------------------------------------------------------
function startReflexes () { stopReflexes(); reflexTimer = setInterval(() => { reflexTick() }, RX.interval) }
function stopReflexes () { if (reflexTimer) { clearInterval(reflexTimer); reflexTimer = null } }

async function reflexTick () {
  if (!bot || !botReady || reflexRunning) return
  reflexRunning = true
  try {
    const fleeThreat = nearestThreat(12)
    // FLEE at low health
    if (bot.health <= RX.fleeHealth && fleeThreat) {
      if (!fleeing) { fleeing = true; emit('auto', { kind: 'flee', from: fleeThreat.name, health: bot.health }); startFlee(fleeThreat) }
      return
    }
    if (fleeing && (bot.health > RX.fleeHealth + 4 || !fleeThreat)) { fleeing = false; try { bot.pathfinder.setGoal(null) } catch (e) {} }
    if (fleeing) return

    // DEFEND (debounced so an unreachable/flickering target can't retrigger every tick)
    if (bot.pvp && bot.pvp.target) return // already fighting
    if (RX.autoDefend) {
      const threat = nearestThreat(RX.defendRadius)
      if (threat) {
        if (Date.now() - lastDefend > 2500) {
          lastDefend = Date.now()
          emit('auto', { kind: 'defend', target: threat.name, dist: round(bot.entity.position.distanceTo(threat.position)) })
          if (bot.pvp) Promise.resolve(bot.pvp.attack(threat)).catch(() => {})
          else manualAttack(threat, 5000).catch(() => {})
        }
        return // a threat is present — don't fall through to idle behaviors
      }
    }

    // EAT
    if (RX.autoEat && bot.food <= RX.eatAt) {
      const food = bestFood()
      if (food) { await reflexEat(food); return }
    }

    // IDLE-only behaviors
    if (isBusy()) return
    if (RX.autoPickup) {
      const item = bot.nearestEntity((e) =>
        e.name === 'item' && e.position &&
        bot.entity.position.distanceTo(e.position) <= RX.pickupRadius)
      if (item) { try { bot.pathfinder.setGoal(new GoalNear(item.position.x, item.position.y, item.position.z, 1)) } catch (e) {} return }
    }
    if (RX.greet) greetNearby()
    if (RX.idleWander && Date.now() - lastWander > RX.wanderInterval) { lastWander = Date.now(); wanderABit() }
  } catch (e) {
    const now = Date.now()
    if (now - lastReflexErr > 5000) { lastReflexErr = now; log('reflex error (rate-limited):', e.message) }
  } finally {
    reflexRunning = false
  }
}

async function reflexEat (food) {
  try { await bot.equip(food, 'hand'); await bot.consume(); emit('auto', { kind: 'ate', item: food.name, food: bot.food }); return true } catch (e) { return false }
}
function startFlee (threat) {
  try { if (bot.pvp) bot.pvp.stop() } catch (e) {}
  const p = bot.entity.position
  let dest
  const owner = OWNER && bot.players[OWNER] && bot.players[OWNER].entity
  if (owner && owner.position && p.distanceTo(owner.position) < 48) dest = owner.position
  else if (threat && threat.position) {
    const away = p.minus(threat.position)
    dest = away.norm() < 0.1 ? p.offset(8, 0, 8) : p.plus(away.normalize().scaled(12))
  } else dest = p.offset(8, 0, 8)
  try { bot.pathfinder.setGoal(new GoalNear(Math.floor(dest.x), Math.floor(dest.y), Math.floor(dest.z), 2), true) } catch (e) {}
}
function greetNearby () {
  const now = Date.now()
  for (const name in bot.players) {
    if (name === bot.username) continue
    const pe = bot.players[name].entity
    if (!pe || !pe.position) continue
    if (bot.entity.position.distanceTo(pe.position) <= 5 && (!greetedAt[name] || now - greetedAt[name] > 120000)) {
      greetedAt[name] = now
      try { bot.chat(`${GREETINGS[Math.floor(Math.random() * GREETINGS.length)]} ${name}`) } catch (e) {}
      emit('auto', { kind: 'greet', player: name })
      return
    }
  }
}
function wanderABit () {
  try {
    const p = bot.entity.position
    const a = Math.random() * Math.PI * 2
    const dx = Math.round(Math.cos(a) * RX.wanderRadius)
    const dz = Math.round(Math.sin(a) * RX.wanderRadius)
    bot.pathfinder.setGoal(new GoalNear(Math.floor(p.x + dx), Math.floor(p.y), Math.floor(p.z + dz), 1))
    emit('auto', { kind: 'wander' })
  } catch (e) {}
}

// ---------------------------------------------------------------------------
// State observation
// ---------------------------------------------------------------------------
function buildState () {
  const e = bot.entity
  const pos = e.position
  const inventory = {}
  for (const it of bot.inventory.items()) inventory[it.name] = (inventory[it.name] || 0) + it.count

  const players = []
  for (const name in bot.players) {
    const pe = bot.players[name].entity
    if (!pe || !pe.position || name === bot.username) continue
    players.push({ username: name, distance: round(pos.distanceTo(pe.position)), position: vec(pe.position) })
  }
  players.sort((a, b) => a.distance - b.distance)

  const entities = []
  for (const id in bot.entities) {
    const en = bot.entities[id]
    if (!en || en === e || en.type === 'player' || !en.position) continue
    const d = pos.distanceTo(en.position)
    if (d > 24) continue
    entities.push({
      name: en.name || en.displayName || en.kind || 'entity', type: en.type,
      hostile: !!(en.name && HOSTILE_NAMES.has(en.name)) || en.type === 'hostile',
      distance: round(d), position: vec(en.position),
    })
  }
  entities.sort((a, b) => a.distance - b.distance)

  const nearbyBlocks = {}
  if (interestIds.length) {
    const found = bot.findBlocks({ matching: interestIds, maxDistance: 16, count: 60 })
    for (const p of found) {
      const b = bot.blockAt(p)
      if (!b) continue
      const d = round(pos.distanceTo(p))
      if (!nearbyBlocks[b.name]) nearbyBlocks[b.name] = { count: 0, nearest: d, nearestPos: vec(p) }
      nearbyBlocks[b.name].count++
      if (d < nearbyBlocks[b.name].nearest) { nearbyBlocks[b.name].nearest = d; nearbyBlocks[b.name].nearestPos = vec(p) }
    }
  }

  let lookingAt = null
  try { const lb = bot.blockAtCursor(5); if (lb) lookingAt = { name: lb.name, position: vec(lb.position) } } catch (e) {}

  return {
    username: bot.username, version: bot.version,
    gameMode: bot.game && bot.game.gameMode, dimension: bot.game && bot.game.dimension,
    position: vec(pos), yaw: round(rad2deg(e.yaw)), pitch: round(rad2deg(e.pitch)),
    health: bot.health, food: bot.food, oxygen: bot.oxygenLevel, onGround: e.onGround,
    memMB: Math.round(process.memoryUsage().heapUsed / 1048576),
    timeOfDay: bot.time && bot.time.timeOfDay,
    isDay: bot.time ? (bot.time.timeOfDay % 24000 < 12300) : null,
    isRaining: bot.isRaining,
    heldItem: bot.heldItem ? bot.heldItem.name : null,
    gear: gearSummary(),
    inventory, lookingAt,
    players: players.slice(0, 8),
    entities: entities.slice(0, 10),
    nearbyBlocks,
    autopilot: {
      owner: OWNER || null,
      fighting: !!(bot.pvp && bot.pvp.target),
      fleeing,
      autoEat: RX.autoEat, autoDefend: RX.autoDefend, autoPickup: RX.autoPickup,
      idleWander: RX.idleWander, greet: RX.greet,
    },
  }
}

// ---------------------------------------------------------------------------
// Actions
// ---------------------------------------------------------------------------
const ACTIONS = {
  status: async () => ({ ready: botReady, username: bot && bot.username, version: bot && bot.version, host: CONFIG.mcHost, port: CONFIG.mcPort }),
  state: async () => { requireBot(); return buildState() },
  inventory: async () => {
    requireBot()
    const inv = {}
    for (const it of bot.inventory.items()) inv[it.name] = (inv[it.name] || 0) + it.count
    return { inventory: inv, held: bot.heldItem ? bot.heldItem.name : null }
  },

  chat: async (args) => {
    requireBot()
    const message = String(args.message == null ? '' : args.message).trim()
    if (!message) return { sent: false, reason: 'empty message' }
    bot.chat(message.slice(0, 256))
    return { sent: true }
  },

  goto: async (args) => {
    requireBot(); stopCombat()
    const x = Math.floor(num(args.x, 'x')); const y = Math.floor(num(args.y, 'y')); const z = Math.floor(num(args.z, 'z'))
    const range = clamp(args.range || 1, 1, 6)
    try { await bot.pathfinder.goto(new GoalNear(x, y, z, range)); return { arrived: true, position: vec(bot.entity.position) } } catch (e) { return { arrived: false, error: e.message, position: vec(bot.entity.position) } }
  },

  gotoPlayer: async (args) => {
    requireBot(); stopCombat()
    const p = bot.players[args.username] && bot.players[args.username].entity
    if (!p || !p.position) throw new Error(`player not visible: ${args.username}`)
    const range = clamp(args.range || 2, 1, 6); const t = p.position
    try { await bot.pathfinder.goto(new GoalNear(t.x, t.y, t.z, range)); return { arrived: true, position: vec(bot.entity.position) } } catch (e) { return { arrived: false, error: e.message, position: vec(bot.entity.position) } }
  },

  follow: async (args) => {
    requireBot(); stopCombat()
    const p = bot.players[args.username] && bot.players[args.username].entity
    if (!p) throw new Error(`player not visible: ${args.username}`)
    const range = clamp(args.range || 3, 1, 8)
    bot.pathfinder.setGoal(new GoalFollow(p, range), true)
    return { following: args.username, range }
  },

  stop: async () => {
    requireBot()
    try { if (bot.pvp) bot.pvp.stop() } catch (e) {}
    try { bot.pathfinder.setGoal(null) } catch (e) {}
    try { bot.pathfinder.stop() } catch (e) {}
    try { bot.stopDigging() } catch (e) {}
    return { stopped: true }
  },

  lookAt: async (args) => {
    requireBot()
    if (args.x != null && args.y != null && args.z != null) {
      await bot.lookAt(new Vec3(Math.floor(num(args.x, 'x')) + 0.5, Math.floor(num(args.y, 'y')) + 0.5, Math.floor(num(args.z, 'z')) + 0.5), true)
    } else if (args.yaw != null || args.pitch != null) {
      await bot.look(deg2rad(args.yaw || 0), deg2rad(args.pitch || 0), true)
    } else throw new Error('lookAt needs x/y/z or yaw/pitch')
    return { looking: true, yaw: round(rad2deg(bot.entity.yaw)), pitch: round(rad2deg(bot.entity.pitch)) }
  },

  mine: async (args) => {
    requireBot(); stopCombat()
    const name = args.name
    const blockDef = mcData.blocksByName[name]
    if (!blockDef) throw new Error(`unknown block name: ${name}`)
    const count = clamp(args.count || 1, 1, 16)
    const maxDistance = clamp(args.maxDistance || 32, 1, 128)
    const deadline = Date.now() + clamp(args.maxMs || 150000, 5000, 170000)
    const positions = bot.findBlocks({ matching: blockDef.id, maxDistance, count })
    if (!positions.length) return { mined: 0, reason: `no ${name} within ${maxDistance} blocks` }
    let mined = 0; let timedOut = false
    for (const pos of positions) {
      if (Date.now() > deadline) { timedOut = true; break }
      const block = bot.blockAt(pos)
      if (!block || block.name !== name) continue
      try {
        await bot.pathfinder.goto(new GoalGetToBlock(pos.x, pos.y, pos.z))
        await equipBestTool(block)
        if (bot.canDigBlock(block)) await bot.dig(block)
        mined++
      } catch (e) {}
      if (mined >= count) break
    }
    try { bot.pathfinder.setGoal(null) } catch (e) {}
    return { mined, requested: count, timedOut }
  },

  place: async (args) => {
    requireBot(); stopCombat()
    const name = args.name
    const item = bot.inventory.items().find((i) => i.name === name)
    if (!item) throw new Error(`no ${name} in inventory`)
    const target = new Vec3(Math.floor(num(args.x, 'x')), Math.floor(num(args.y, 'y')), Math.floor(num(args.z, 'z')))
    const targetBlock = bot.blockAt(target)
    if (targetBlock && targetBlock.boundingBox === 'block') return { placed: false, error: 'target cell is already occupied — pick an empty cell (air)' }
    let approachErr = null
    try { await bot.pathfinder.goto(new GoalNear(target.x, target.y, target.z, 3)) } catch (e) { approachErr = e.message }
    await bot.equip(item, 'hand')
    const faces = [[0, -1, 0], [0, 1, 0], [1, 0, 0], [-1, 0, 0], [0, 0, 1], [0, 0, -1]]
    let lastErr = null
    for (const [dx, dy, dz] of faces) {
      const ref = bot.blockAt(target.offset(dx, dy, dz))
      if (ref && ref.boundingBox === 'block' && ref.name !== 'air') {
        try { await bot.lookAt(target.offset(0.5, 0.5, 0.5), true); await bot.placeBlock(ref, new Vec3(-dx, -dy, -dz)); return { placed: true, at: { x: target.x, y: target.y, z: target.z } } } catch (e) { lastErr = e }
      }
    }
    const why = lastErr ? lastErr.message : 'no solid block adjacent to place against'
    return { placed: false, error: approachErr ? `${why} (approach failed: ${approachErr})` : why }
  },

  collect: async (args) => {
    requireBot(); stopCombat()
    const maxDistance = clamp(args.maxDistance || 16, 1, 48)
    let visited = 0
    for (let i = 0; i < 16; i++) {
      const item = bot.nearestEntity((en) => en.name === 'item' && en.position && bot.entity.position.distanceTo(en.position) <= maxDistance)
      if (!item) break
      const p = item.position
      try { await bot.pathfinder.goto(new GoalNear(p.x, p.y, p.z, 1)); visited++; await sleep(200) } catch (e) { break }
    }
    try { bot.pathfinder.setGoal(null) } catch (e) {}
    return { walkedTo: visited }
  },

  // High-level "gather" primitive: auto-pick the nearest useful resource, mine it,
  // collect the drop. The LLM does not need to choose a block name or coordinate.
  harvestNearest: async (args) => {
    requireBot(); stopCombat()
    if (!resourceIds.length) return { total: 0, reason: 'no resource types known yet' }
    const maxDistance = clamp(args.maxDistance || 32, 1, 128)
    const count = clamp(args.count || 1, 1, 16)
    const deadline = Date.now() + clamp(args.maxMs || 90000, 5000, 170000)
    const harvested = {}
    let total = 0
    let stuckKey = null
    for (let i = 0; i < count; i++) {
      if (Date.now() > deadline) break
      const block = bot.findBlock({ matching: resourceIds, maxDistance })
      if (!block) break
      const key = `${block.position.x},${block.position.y},${block.position.z}`
      if (key === stuckKey) break // couldn't make progress on the nearest block
      try {
        await bot.pathfinder.goto(new GoalGetToBlock(block.position.x, block.position.y, block.position.z))
        await equipBestTool(block)
        if (bot.canDigBlock(block)) await bot.dig(block)
        harvested[block.name] = (harvested[block.name] || 0) + 1
        total++
        stuckKey = null
      } catch (e) { stuckKey = key }
    }
    try { bot.pathfinder.setGoal(null) } catch (e) {}
    return { total, harvested }
  },

  // High-level "stash" primitive: walk to the nearest chest and deposit all
  // gathered resources, keeping tools/food/armor.
  stashResources: async () => {
    requireBot(); stopCombat()
    const chestBlock = bot.findBlock({ matching: (b) => b && /(^|_)chest$/.test(b.name) && b.name !== 'ender_chest', maxDistance: 48 })
    if (!chestBlock) return { ok: false, reason: 'no chest within 48 blocks' }
    try { await bot.pathfinder.goto(new GoalGetToBlock(chestBlock.position.x, chestBlock.position.y, chestBlock.position.z)) } catch (e) { return { ok: false, error: 'could not reach the chest' } }
    let chest
    try { chest = await bot.openContainer(chestBlock) } catch (e) { return { ok: false, error: 'could not open the chest' } }
    const deposited = {}
    const items = bot.inventory.items().map((it) => ({ type: it.type, name: it.name, count: it.count }))
    try {
      for (const it of items) {
        if (isKeepItem(it.name)) continue
        try { await chest.deposit(it.type, null, it.count); deposited[it.name] = (deposited[it.name] || 0) + it.count } catch (e) { /* chest full / can't deposit this one */ }
      }
    } finally { try { chest.close() } catch (e) {} }
    return { deposited, at: vec(chestBlock.position) }
  },

  attack: async (args) => {
    requireBot()
    const target = resolveTarget(args.target)
    if (!target) return { engaged: false, reason: `no target matching "${args.target || 'hostile'}"` }
    try { bot.pathfinder.setGoal(null) } catch (e) {}
    if (bot.pvp) { Promise.resolve(bot.pvp.attack(target)).catch(() => {}); return { engaged: true, target: describeEntity(target), via: 'pvp' } }
    manualAttack(target, clamp(args.maxMs || 6000, 1000, 20000)).catch(() => {})
    return { engaged: true, target: describeEntity(target), via: 'manual' }
  },

  equip: async (args) => {
    requireBot()
    const item = bot.inventory.items().find((i) => i.name === args.name)
    if (!item) throw new Error(`no ${args.name} in inventory`)
    const dest = args.dest || 'hand'
    await bot.equip(item, dest)
    return { equipped: args.name, dest }
  },

  drop: async (args) => {
    requireBot()
    const item = bot.inventory.items().find((i) => i.name === args.name)
    if (!item) throw new Error(`no ${args.name} in inventory`)
    const count = args.count ? clamp(args.count, 1, item.count) : null
    await bot.toss(item.type, null, count)
    return { dropped: args.name, count: count == null ? 'all' : count }
  },

  eat: async (args) => {
    requireBot()
    const item = args.name ? bot.inventory.items().find((i) => i.name === args.name) : bestFood()
    if (!item) return { ate: false, reason: args.name ? `no ${args.name}` : 'no food in inventory' }
    await bot.equip(item, 'hand')
    try { await bot.consume(); return { ate: true, item: item.name, food: bot.food } } catch (e) { return { ate: false, error: e.message } }
  },

  flee: async (args) => {
    requireBot()
    const threat = resolveTarget(args.target || 'hostile')
    fleeing = true
    startFlee(threat)
    return { fleeing: true, from: threat ? describeEntity(threat) : null }
  },

  sleep: async () => {
    requireBot()
    const bed = bot.findBlock({ matching: (b) => b && b.name && b.name.includes('bed'), maxDistance: 16 })
    if (!bed) return { slept: false, reason: 'no bed within 16 blocks' }
    try { await bot.pathfinder.goto(new GoalGetToBlock(bed.position.x, bed.position.y, bed.position.z)) } catch (e) {}
    try { await bot.sleep(bed); return { slept: true } } catch (e) { return { slept: false, error: e.message } }
  },

  craft: async (args) => {
    requireBot(); stopCombat()
    const name = args.name
    if (TOOL_SPECS[name]) return await makeTool(name) // tools auto-make planks/sticks/table
    const itemDef = mcData.itemsByName[name]
    if (!itemDef) throw new Error(`unknown item: ${name}`)
    const count = clamp(args.count || 1, 1, 64)
    const table = nearbyTable() || await ensureTable()
    const recipes = bot.recipesFor(itemDef.id, null, 1, table)
    if (!recipes.length) return { crafted: false, reason: `missing ingredients for ${name}` }
    try { await bot.craft(recipes[0], count, table || undefined); return { crafted: true, item: name, count } } catch (e) { return { crafted: false, error: e.message } }
  },

  // Self-analyze gear and take ONE step up the ladder per call:
  // wooden pickaxe -> stone tools -> furnace -> smelt iron -> iron pickaxe/sword
  // -> iron armor. Reports what raw material to gather if short, so the caller
  // can harvestNearest (wood/stone/iron_ore/coal) and call gearUp again.
  gearUp: async () => {
    requireBot(); stopCombat()
    const g = gearSummary()

    // 1) wood + stone tool ladder
    let target = null
    if (!g.pickaxe) target = 'wooden_pickaxe'
    else if (g.pickaxe === 'wooden') target = 'stone_pickaxe'
    else if (!g.sword) target = 'stone_sword'
    else if (!g.axe) target = 'stone_axe'
    if (target) { const res = await makeTool(target); return { stage: 'stone-tools', target, ...res, gear: gearSummary() } }

    // 2) build a furnace once we have cobblestone (and don't already have one)
    if (!g.furnace && g.cobblestone >= 8) {
      const f = await ensureFurnace()
      if (f) return { stage: 'furnace', target: 'furnace', ok: true, gear: gearSummary() }
      // ensureFurnace crafts before placing, so a furnace item now on hand means
      // the craft worked but there was nowhere to place it — don't ask for cobble.
      return invCount('furnace') > 0
        ? { stage: 'furnace', target: 'furnace', ok: false, need: 'space', reason: 'crafted a furnace but no room to place it — move to open ground', gear: gearSummary() }
        : { stage: 'furnace', target: 'furnace', ok: false, need: 'cobblestone', reason: 'could not build a furnace', gear: gearSummary() }
    }

    // 3) smelt raw iron into ingots
    if (g.rawIron > 0 && (g.furnace || g.cobblestone >= 8)) {
      const res = await smeltIron(8)
      return { stage: 'smelt', ...res, gear: gearSummary() }
    }

    // 4) upgrade pickaxe/sword to iron
    if (g.pickaxe === 'stone' && g.iron >= 3) { const res = await makeTool('iron_pickaxe'); return { stage: 'iron-tools', target: 'iron_pickaxe', ...res, gear: gearSummary() } }
    if (g.sword === 'stone' && g.iron >= 2) { const res = await makeTool('iron_sword'); return { stage: 'iron-tools', target: 'iron_sword', ...res, gear: gearSummary() } }

    // 5) craft the best armor piece we can afford and don't already have
    for (const name of ARMOR_PRIORITY) {
      const spec = ARMOR_SPECS[name]
      if (!g.armor[spec.key] && g.iron >= spec.iron) {
        const res = await makeArmor(name)
        return { stage: 'armor', target: name, ...res, gear: gearSummary() }
      }
    }

    // 6) nothing craftable right now — report the next material to gather
    return nextGearNeed(g)
  },

  depositChest: async (args) => {
    requireBot()
    const chest = await openNearestChest()
    if (!chest) return { ok: false, reason: 'no chest within 16 blocks' }
    try {
      const item = bot.inventory.items().find((i) => i.name === args.name)
      if (!item) { chest.close(); return { ok: false, reason: `no ${args.name} to deposit` } }
      const count = args.count ? clamp(args.count, 1, item.count) : item.count
      await chest.deposit(item.type, null, count)
      chest.close()
      return { deposited: args.name, count }
    } catch (e) { try { chest.close() } catch (x) {} return { ok: false, error: e.message } }
  },

  withdrawChest: async (args) => {
    requireBot()
    const chest = await openNearestChest()
    if (!chest) return { ok: false, reason: 'no chest within 16 blocks' }
    try {
      const itemDef = mcData.itemsByName[args.name]
      if (!itemDef) { chest.close(); return { ok: false, reason: `unknown item ${args.name}` } }
      const count = clamp(args.count || 1, 1, 64)
      await chest.withdraw(itemDef.id, null, count)
      chest.close()
      return { withdrew: args.name, count }
    } catch (e) { try { chest.close() } catch (x) {} return { ok: false, error: e.message } }
  },

  // -- reflex / autopilot control (no requireBot; safe anytime) --
  setReflexes: async (args) => {
    for (const k of ['autoEat', 'autoDefend', 'autoPickup', 'idleWander', 'greet']) if (args[k] != null) RX[k] = !!args[k]
    for (const k of ['eatAt', 'defendRadius', 'fleeHealth', 'pickupRadius', 'wanderRadius', 'wanderInterval']) if (args[k] != null) RX[k] = Number(args[k])
    return { reflexes: { ...RX } }
  },
  setOwner: async (args) => { OWNER = args.username || ''; return { owner: OWNER || null } },
}

// ---------------------------------------------------------------------------
// Shutdown
// ---------------------------------------------------------------------------
function shutdown () {
  shuttingDown = true
  stopReflexes()
  try { if (bot) bot.quit('controller shutting down') } catch (e) {}
  try { server.close() } catch (e) {}
  process.exit(0)
}
process.on('SIGINT', shutdown)
process.on('SIGTERM', shutdown)

// Memory watchdog — logs the breakdown so growth is visible (chunks vs heap vs
// buffers), and forces GC when heap runs high (node is launched with --expose-gc).
setInterval(() => {
  try {
    if (!bot) return
    const m = process.memoryUsage()
    const mb = (b) => Math.round(b / 1048576)
    let chunks = '?'
    try {
      const w = bot.world
      if (w && w.columns) chunks = Object.keys(w.columns).length
      else if (w && typeof w.getColumns === 'function') chunks = w.getColumns().length
    } catch (e) {}
    const ents = bot.entities ? Object.keys(bot.entities).length : '?'
    log(`mem heap=${mb(m.heapUsed)} rss=${mb(m.rss)} ext=${mb(m.external)} arr=${mb(m.arrayBuffers)} MB | chunks=${chunks} entities=${ents}`)
    if (global.gc && m.heapUsed > 1500 * 1048576) { global.gc(); log('forced GC') }
  } catch (e) {}
}, parseInt(process.env.MEM_LOG_MS || '10000', 10))

createBot()
