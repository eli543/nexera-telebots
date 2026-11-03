// Telebots feature module. Importing this file does not start a bot, read state,
// register handlers, spawn a process, or write files. The seam calls init first.
import { Composer, GrammyError, InlineKeyboard, type Context } from 'grammy'
import type { InlineKeyboardMarkup, ReactionType, ReactionTypeEmoji, UserFromGetMe } from 'grammy/types'
import { appendFileSync, existsSync, lstatSync, mkdirSync, readFileSync, realpathSync, renameSync, rmdirSync, statSync, unlinkSync, writeFileSync } from 'node:fs'
import { execFile, execFileSync, spawn } from 'node:child_process'
import { basename, isAbsolute, join, resolve } from 'node:path'
import { homedir } from 'node:os'
import { createHash, randomBytes } from 'node:crypto'
import { EmailService } from './email.ts'
import type { CallToolResult, Tool } from '@modelcontextprotocol/sdk/types.js'
import type { AttachmentMeta, EventFields, ExecOptions, ForwardParams, ReplyMarkup, Telebots, TelebotsDependencies } from './contracts.ts'

export type UX = {
  reactions: Array<{ emoji: string; phrase: string; key_line: string }>
  commands: Array<{ command: string; description: string }>
  quick_bar: string[][]
  key_extra: string[]
}

// Compiled defaults keep the guarded import usable when the optional installed
// UX file is absent. The test suite checks this table against ux.json.
export const DEFAULT_UX: UX = {
  reactions: [
    { emoji: '❤', phrase: 'Save everything to my notes', key_line: '❤️ save everything to your notes folder' },
    { emoji: '👍', phrase: 'Yes, go ahead', key_line: '👍 yes, go ahead' },
    { emoji: '👎', phrase: 'No, stop', key_line: '👎 no, stop' },
    { emoji: '🔥', phrase: 'Priority: put this at the top of my list', key_line: '🔥 priority: put this at the top of my list' },
    { emoji: '👏', phrase: 'Wrap it up: done, then save everything', key_line: '👏 wrap it up: mark it done, then save everything' },
    { emoji: '😁', phrase: 'Explain this simply', key_line: '😁 explain it simply' },
    { emoji: '🥰', phrase: 'Watch this', key_line: '🥰 watch this: buzz me when it changes' },
  ],
  commands: [
    { command: 'esc', description: 'Interrupt the current task - read my newest message now' },
    { command: 'fresh', description: 'Restart this bot blank (fresh context, default model)' },
    { command: 'update', description: 'Reload the installed build (saved chat kept; no download)' },
    { command: 'key', description: 'The key: what every reaction, bubble and command does' },
    { command: 'inbox', description: 'Your top threads as cards: Reply / Archive / Snooze by tap' },
    { command: 'opus', description: 'Switch to Opus, keeping saved chat and current or default effort' },
    { command: 'fable', description: 'Switch to Fable, keeping saved chat and current or default effort' },
    { command: 'model', description: 'Show or switch model + effort (e.g. /model fable high)' },
    { command: 'effort', description: 'Pick effort for this chat: low / medium / high / xhigh / max (conversation kept)' },
    { command: 'context', description: 'Show context usage - how full this chat is' },
    { command: 'compact', description: "Compact this bot's context (optional focus after it)" },
    { command: 'usage', description: 'Plan limits left + fleet token burn (today / 7 days)' },
    { command: 'resume', description: 'List recent chats across all bots / reopen one' },
  ],
  quick_bar: [['/esc', '/model'], ['/usage', '/fresh']],
  key_extra: [
    'Bubbles under a message: a tap is your answer.',
    'Email goes out only after an exact preview, ✉️ Send it, then ✅ Confirm send. Reactions and ordinary bubbles cannot authorize email.',
    'Handoff: at 60 / 70 / 80 / 90% full, pick another bot, review the handoff, then confirm. A single-bot fleet offers Keep going and Compact now.',
  ],
}

const EFFORTS = ['low', 'medium', 'high', 'xhigh', 'max'] as const
type Effort = typeof EFFORTS[number]
const MODELS = ['opus', 'fable', 'sonnet', 'haiku'] as const
const MODEL_LABELS: Record<string, string> = { opus: 'Opus', fable: 'Fable', sonnet: 'Sonnet', haiku: 'Haiku' }
const KEY_RE = /^[a-z0-9][a-z0-9-]{0,30}$/
const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i
const RESUME_RE = /^rs:([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$/i
const EFFORT_RE = /^eff:(low|medium|high|xhigh|max|close)$/
const MODEL_RE = /^md:(opus|fable|sonnet|haiku)$/
const MODEL_EFFORT_RE = /^me:(opus|fable|sonnet|haiku):(low|medium|high|xhigh|max)$/
const QUICK_RE = /^qr:(\d{1,2}):(\d{1,2})$/
const HANDOFF_RE = /^ho:(?:bot:)?([a-z0-9][a-z0-9-]{0,30})$/
const NOTE_ID_RE = /^[a-f0-9]{24}$/
const DELIVERY_LIFETIME_MS = 60 * 60 * 1000
type DeliveryRecord = { delivery_id: string; note_id: string; source: string; target: string; issued_at: number; digest: string; phase: 'issued' | 'sent' | 'received' | 'cancelled' }
const HANDOFF_RECEIPT_TOOL: Tool = {
  name: 'handoff_receipt',
  description: 'The sender calls issue before the first preview. If the note changes or the one-hour ID expires before sending, call renew with the old ID, then show a fresh preview; the old ID is cancelled. After SendMessage reports delivery, call sent with the current ID. The receiver calls received only after getting the full note. These are agent attestations, not independent transport proof.',
  inputSchema: { type: 'object', additionalProperties: false, properties: {
    note_id: { type: 'string' }, phase: { type: 'string', enum: ['issue', 'renew', 'sent', 'received'] }, delivery_id: { type: 'string' },
  }, required: ['note_id', 'phase'] },
}
const isEffort = (value: string): value is Effort => (EFFORTS as readonly string[]).includes(value)
const readText = (path: string): string => { try { return readFileSync(path, 'utf8') } catch { return '' } }
const safeMeta = (s: string): string => s.replace(/[<>\[\]\r\n;]/g, '_')

export function keyText(ux: UX = DEFAULT_UX): string {
  return ['🔑 Key', 'React to any bot message:\n' + ux.reactions.map(r => r.key_line).join('\n'),
    ux.key_extra[0], 'Quick bar: ' + ux.quick_bar.map(row => row.join(' · ')).join(' / '),
    'Menu:\n' + ux.commands.map(c => `/${c.command} - ${c.description}`).join('\n'),
    ...ux.key_extra.slice(1)].join('\n\n')
}

function loadUX(fleetHome: string): UX {
  try {
    const candidate = JSON.parse(readText(join(fleetHome, 'ux.json'))) as UX
    if (candidate.commands?.length !== 13 || candidate.reactions?.length !== 7 || !Array.isArray(candidate.quick_bar) || !Array.isArray(candidate.key_extra)) throw new Error('shape')
    if (!candidate.commands.every((c, i) => c.command === DEFAULT_UX.commands[i]?.command && typeof c.description === 'string' && c.description.length > 0 && c.description.length <= 256)) throw new Error('commands')
    if (!candidate.reactions.every((r, i) => r.emoji === DEFAULT_UX.reactions[i]?.emoji && typeof r.phrase === 'string' && typeof r.key_line === 'string')) throw new Error('reactions')
    if (!candidate.quick_bar.every(row => Array.isArray(row) && row.every(t => typeof t === 'string')) || !candidate.key_extra.every(t => typeof t === 'string')) throw new Error('strings')
    return candidate
  } catch { return structuredClone(DEFAULT_UX) }
}

export function replyButtons(raw: unknown): InlineKeyboard | undefined {
  if (raw === undefined || raw === null) return undefined
  if (!Array.isArray(raw)) throw new Error('buttons must be an array of 1-6 short label strings')
  if (raw.length > 6) throw new Error(`too many buttons: ${raw.length} (1-6 allowed)`)
  for (const [i, value] of raw.entries()) {
    if (typeof value === 'string' && /[\p{Cc}\p{Zl}\p{Zp}]/u.test(value)) throw new Error(`button ${i + 1} contains a newline or control character`)
  }
  const labels = raw.map(value => typeof value === 'string' ? value.trim() : '')
  for (const [i, label] of labels.entries()) {
    if (!label) throw new Error(`button ${i + 1} is empty - each label needs 1-40 characters`)
    if (label.length > 40) throw new Error(`button ${i + 1} is ${label.length} characters - the limit is 40`)
  }
  if (!labels.length) return undefined
  const longest = Math.max(...labels.map(label => label.length))
  const width = longest <= 12 ? 3 : longest <= 18 ? 2 : 1
  const keyboard = new InlineKeyboard()
  labels.forEach((label, index) => {
    if (index && index % width === 0) keyboard.row()
    keyboard.text(label, `qr:${Math.floor(index / width)}:${index % width}`)
  })
  return keyboard
}

export function addedReactionEmoji(before: ReadonlyArray<ReactionType>, after: ReadonlyArray<ReactionType>): string[] {
  const normalize = (emoji: string) => emoji.replace(/\uFE0F/g, '')
  const old = new Set(before.filter(r => r.type === 'emoji').map(r => normalize(r.emoji)))
  return [...new Set(after.filter(r => r.type === 'emoji').map(r => normalize(r.emoji)))].filter(emoji => !old.has(emoji))
}

export function shq(value: string): string { return `'${value.replace(/'/g, `'\\''`)}'` }

export function validatedApiRoot(value: string | undefined, testMode = process.env.TELEBOTS_TEST === '1'): string {
  if (testMode && value && /^https?:\/\/(?:127\.0\.0\.1|localhost|\[::1\])(?::[0-9]+)?\/?$/.test(value)) {
    try {
      const url = new URL(value)
      if (['http:', 'https:'].includes(url.protocol) && ['127.0.0.1', 'localhost', '[::1]'].includes(url.hostname) && !url.username && !url.password && !url.search && !url.hash && url.pathname === '/') return url.origin
    } catch {}
  }
  return 'https://api.telegram.org'
}

function execOutput(file: string, args: string[], options: ExecOptions): Promise<{ stdout: string }> {
  return new Promise((yes, no) => {
    execFile(file, args, { ...options, encoding: 'utf8' }, (err, stdout) => {
      if (err) { no(Object.assign(err, { stdout: String(stdout ?? '') })); return }
      yes({ stdout: String(stdout ?? '') })
    })
  })
}

// fleet-esc and fleet-compact press a lone Esc with the TIOCSTI ioctl, which the kernel allows
// only on the caller's own controlling terminal. They start without `detached`, so they inherit
// this poller's session and the bot's terminal; nohup and the shell's & still keep them running
// on their own. Every other engine starts in its own session, as before.
export const IN_SESSION_ENGINES = /^fleet-(?:esc|compact)$/

export function engineSpawnOptions(file: string): { detached: boolean; stdio: 'ignore' } {
  return { detached: !IN_SESSION_ENGINES.test(basename(file)), stdio: 'ignore' }
}

function spawnEngine(file: string, args: string[], logFile: string): void {
  const command = `nohup ${[file, ...args].map(shq).join(' ')} </dev/null >>${shq(logFile)} 2>&1 &`
  const child = spawn('/bin/sh', ['-c', command], engineSpawnOptions(file))
  child.on('error', () => {})
  child.unref()
}

function executable(path: string): boolean {
  try { const stat = statSync(path); return stat.isFile() && (stat.mode & 0o111) !== 0 } catch { return false }
}

export function findOwningClaudePid(): number | null {
  let pid = process.ppid
  for (let hop = 0; hop < 8 && pid > 1; hop++) {
    let line: string
    try { line = execFileSync('ps', ['-o', 'ppid=,command=', '-p', String(pid)], { encoding: 'utf8', stdio: ['ignore', 'pipe', 'ignore'] }).trim() } catch { return null }
    const match = /^(\d+)\s+(.+)$/.exec(line)
    if (!match) return null
    const command = match[2]!
    // The nearest real CLI decides; never jump past an intervening mcp-list load.
    if (basename(command.split(' ')[0]!) === 'claude') return /(?:^|\s)--channels(?:\s|=)/.test(command) && command.includes('plugin:telegram') ? pid : null
    pid = Number(match[1])
  }
  return null
}

type FleetBot = { key: string; name: string; state_dir: string; workdir: string; emoji?: string }
type FleetRow = { key?: string; name?: string; state_name?: string; state_dir?: string; up?: boolean; busy?: boolean; context_pct?: number | null }
type TranscriptStatus = 'ok' | 'empty' | 'skipped' | 'failed' | 'timeout'

export function formatVoiceDuration(seconds: number): string {
  const total = Math.max(0, Math.round(seconds))
  const h = Math.floor(total / 3600), m = Math.floor((total % 3600) / 60), s = String(total % 60).padStart(2, '0')
  return h > 0 ? `${h}:${String(m).padStart(2, '0')}:${s}` : `${m}:${s}`
}

export function voiceBody(status: TranscriptStatus, kind: string, seconds: number, transcript: string, caption: string | undefined, fallback: string): string {
  if (status !== 'ok' && status !== 'empty') return fallback
  const note = status === 'ok' ? 'transcribed on device' : 'no speech detected'
  const header = `(${kind === 'audio' ? 'audio file' : 'voice message'}, ${formatVoiceDuration(seconds)}, ${note})`
  return header + (status === 'ok' ? `\n\n${transcript}` : '') + (caption ? `\n\nCaption: ${caption}` : '')
}

export function init(deps: TelebotsDependencies): Telebots { return new TelebotsModule(deps) }

export class TelebotsModule implements Telebots {
  readonly allowedUpdates: Array<'message' | 'callback_query' | 'message_reaction'> = ['message', 'callback_query', 'message_reaction']
  readonly ux: UX
  readonly email: EmailService
  readonly fleetHome: string
  private readonly now: () => number
  private readonly runOutput: NonNullable<TelebotsDependencies['runOutput']>
  private readonly detached: NonNullable<TelebotsDependencies['spawnDetached']>
  private readonly chains = new Map<string, Promise<void>>()
  private readonly keyboards = new Map<string, InlineKeyboardMarkup | null>()
  private readonly pending = new Set<string>()
  private readonly timers = new Set<ReturnType<typeof setTimeout>>()
  private readonly tasks = new Set<Promise<unknown>>()
  private readonly quickBar: Exclude<ReplyMarkup, InlineKeyboard | undefined>
  private quickBarSent = false
  private registered = false
  private disposed = false
  private resumeOpenInFlight = false

  constructor(private readonly deps: TelebotsDependencies) {
    if (!isAbsolute(deps.stateDir)) throw new Error('stateDir must be absolute')
    this.fleetHome = this.expand(deps.fleetHome ?? process.env.FLEET_HOME ?? join(homedir(), '.claude', 'telegram-fleet'))
    this.now = deps.now ?? Date.now
    this.runOutput = deps.runOutput ?? execOutput
    this.detached = deps.spawnDetached ?? spawnEngine
    this.ux = loadUX(this.fleetHome)
    this.quickBar = { keyboard: this.ux.quick_bar.map(row => row.map(text => ({ text }))), resize_keyboard: true, is_persistent: true, one_time_keyboard: false }
    this.email = new EmailService({ bot: deps.bot, stateDir: deps.stateDir, fleetHome: this.fleetHome,
      botKey: () => this.self()?.key, assertAllowedChat: deps.assertAllowedChat, now: this.now,
      logEvent: (event, fields) => this.logEvent(event, fields), allowedCallback: ctx => this.allowedCallback(ctx),
      injectConfirm: async (ctx, consentId) => { this.inject(ctx, 'Confirm send', 'email_confirm', { consent_id: consentId }) },
    })
  }

  get tools() { return [...this.email.tools, HANDOFF_RECEIPT_TOOL] }
  async callTool(name: string, args: Record<string, unknown>): Promise<CallToolResult | null> {
    if (name !== 'handoff_receipt') return this.email.callTool(name, args)
    try {
      const id = args.note_id, phase = args.phase
      if (typeof id !== 'string' || !NOTE_ID_RE.test(id) || !['issue', 'renew', 'sent', 'received'].includes(String(phase))) throw Error('Invalid handoff receipt.')
      const self = this.self()?.key
      if (!self) throw Error('This bot is not configured.')
      const path = join(this.fleetHome, 'handoffs', `${id}.md`)
      const info = lstatSync(path)
      if (!info.isFile() || (info.mode & 0o777) !== 0o600 || info.size > 64_000) throw Error('Invalid handoff note.')
      const note = readFileSync(path, 'utf8')
      const source = /^SOURCE-KEY: ([a-z0-9][a-z0-9-]{0,30})$/m.exec(note)?.[1]
      const target = /^TARGET-KEY: ([a-z0-9][a-z0-9-]{0,30})$/m.exec(note)?.[1]
      if (!note.startsWith('HANDOFF from ') || !note.includes(`\nNOTE-ID: ${id}\n`) || !source || !target || source === target ||
          (phase === 'received' ? target !== self : source !== self)) throw Error('Invalid handoff note.')
      const directory = join(this.fleetHome, 'handoffs', 'deliveries')
      if (phase === 'issue') {
        if (args.delivery_id !== undefined || /^DELIVERY-ID:/m.test(note)) throw Error('Delivery ID already exists.')
        mkdirSync(directory, { recursive: true, mode: 0o700 })
        const delivery = randomBytes(12).toString('hex')
        const updated = note + (note.endsWith('\n') ? '' : '\n') + `DELIVERY-ID: ${delivery}\n`
        if (Buffer.byteLength(updated) > 64_000) throw Error('Handoff note is too large.')
        const record: DeliveryRecord = { delivery_id: delivery, note_id: id, source, target,
          issued_at: this.now(), digest: createHash('sha256').update(updated).digest('hex'), phase: 'issued' }
        const receiptPath = join(directory, `${delivery}.json`)
        writeFileSync(receiptPath, JSON.stringify(record) + '\n', { flag: 'wx', mode: 0o600 })
        try { writeFileSync(path, updated, { mode: 0o600 }) } catch (error) { unlinkSync(receiptPath); throw error }
        this.logEvent('HANDOFF-ISSUED', { id, source, target, delivery })
        return { content: [{ type: 'text', text: `Delivery ID: ${delivery}. Preview the updated note before sending.` }] }
      }
      const delivery = args.delivery_id
      if (typeof delivery !== 'string' || !NOTE_ID_RE.test(delivery) || !note.includes(`\nDELIVERY-ID: ${delivery}\n`)) throw Error('Invalid delivery ID.')
      const receiptPath = join(directory, `${delivery}.json`)
      const lock = `${receiptPath}.lock`
      mkdirSync(lock, { mode: 0o700 })
      try {
        const receiptInfo = lstatSync(receiptPath)
        if (!receiptInfo.isFile() || (receiptInfo.mode & 0o777) !== 0o600 || receiptInfo.size > 4096) throw Error('Invalid delivery record.')
        const record = JSON.parse(readFileSync(receiptPath, 'utf8')) as DeliveryRecord
        if (record.delivery_id !== delivery || record.note_id !== id || record.source !== source || record.target !== target ||
            !Number.isFinite(record.issued_at) || record.issued_at > this.now()) throw Error('Invalid delivery record.')
        if (phase === 'renew') {
          const idLines = note.match(/^DELIVERY-ID: [^\r\n]*$/gm) ?? []
          if (record.phase !== 'issued' || idLines.length !== 1 || idLines[0] !== `DELIVERY-ID: ${delivery}`) throw Error('Invalid delivery record.')
          const replacement = randomBytes(12).toString('hex')
          const updated = note.replace(`\nDELIVERY-ID: ${delivery}\n`, `\nDELIVERY-ID: ${replacement}\n`)
          if (updated === note || Buffer.byteLength(updated) > 64_000) throw Error('Invalid handoff note.')
          const next: DeliveryRecord = { delivery_id: replacement, note_id: id, source, target,
            issued_at: this.now(), digest: createHash('sha256').update(updated).digest('hex'), phase: 'issued' }
          const nextPath = join(directory, `${replacement}.json`)
          writeFileSync(nextPath, JSON.stringify(next) + '\n', { flag: 'wx', mode: 0o600 })
          const oldTemp = `${receiptPath}.${process.pid}.${randomBytes(4).toString('hex')}.tmp`
          const noteTemp = `${path}.${process.pid}.${randomBytes(4).toString('hex')}.tmp`
          try {
            writeFileSync(oldTemp, JSON.stringify({ ...record, phase: 'cancelled' }) + '\n', { flag: 'wx', mode: 0o600 })
            renameSync(oldTemp, receiptPath)
            writeFileSync(noteTemp, updated, { flag: 'wx', mode: 0o600 })
            renameSync(noteTemp, path)
          } finally {
            try { unlinkSync(oldTemp) } catch {}
            try { unlinkSync(noteTemp) } catch {}
          }
          this.logEvent('HANDOFF-ISSUED', { id, source, target, delivery: replacement })
          return { content: [{ type: 'text', text: `Delivery ID: ${replacement}. The old ID is cancelled. Preview the updated note again before sending.` }] }
        }
        if (this.now() - record.issued_at > DELIVERY_LIFETIME_MS ||
            record.digest !== createHash('sha256').update(note).digest('hex') ||
            record.phase !== (phase === 'sent' ? 'issued' : 'sent')) throw Error('Invalid delivery record.')
        record.phase = phase as 'sent' | 'received'
        const temporary = `${receiptPath}.${process.pid}.${randomBytes(4).toString('hex')}.tmp`
        try {
          writeFileSync(temporary, JSON.stringify(record) + '\n', { flag: 'wx', mode: 0o600 })
          renameSync(temporary, receiptPath)
        } finally { try { unlinkSync(temporary) } catch {} }
      } finally { rmdirSync(lock) }
      this.logEvent(phase === 'sent' ? 'HANDOFF-SENT' : 'HANDOFF-RECEIVED', { id, source, target, delivery })
      return { content: [{ type: 'text', text: `Agent-attested handoff ${phase} recorded for note ${id}.` }] }
    } catch {
      return { isError: true, content: [{ type: 'text', text: 'Handoff receipt refused. Check the local note ID and destination.' }] }
    }
  }
  beforeEdit(chatId: string, messageId: string | number): void {
    this.keyboards.set(`${chatId}:${messageId}`, null)
    this.email.beforeEdit(chatId, messageId)
  }
  replyButtons = replyButtons
  replyMarkup(buttons: InlineKeyboard | undefined, index: number, total: number): ReplyMarkup {
    return buttons && index === total - 1 ? buttons : !buttons && !this.quickBarSent && index === 0 ? this.quickBar : undefined
  }
  replySent(markup: ReplyMarkup): void { if (markup === this.quickBar) this.quickBarSent = true }

  logEvent(event: string, fields: EventFields = {}): void {
    // Allow only evidence fields, never message text, arbitrary errors, paths or tokens.
    const keys = new Set(['n', 'kind', 'cmd', 'family', 'cell', 'emoji', 'id', 'read', 'source', 'target', 'delivery', 'status', 'offered', 'engine', 'ok', 'count', 'labels'])
    const values = Object.entries(fields).filter(([key]) => keys.has(key)).map(([key, value]) => `${key}=${String(value).replace(/[\s\x00-\x1f]/g, '_').slice(0, 200)}`)
    try {
      mkdirSync(join(this.deps.stateDir, 'logs'), { recursive: true, mode: 0o700 })
      appendFileSync(join(this.deps.stateDir, 'logs', 'poller-events.log'), `${new Date(this.now()).toISOString()} ${event.replace(/[^A-Z0-9-]/g, '')}${values.length ? ' ' + values.join(' ') : ''}\n`, { mode: 0o600 })
    } catch {}
  }

  afterPidWrite(): void {
    if (readText(join(this.deps.stateDir, 'bot.pid')).trim() !== String(process.pid)) return
    try { writeFileSync(join(this.deps.stateDir, 'caps'), `resume-buttons=1\npid=${process.pid}\n`, { mode: 0o600 }) } catch {}
  }

  async onPollStart(_info: UserFromGetMe): Promise<void> {
    this.logEvent('START')
    try {
      await this.deps.bot.api.setMyCommands(this.ux.commands, { scope: { type: 'all_private_chats' } })
      this.logEvent('MENU-SET', { n: this.ux.commands.length })
    } catch {}
  }

  private expand(path: string): string { return resolve(path.startsWith('~/') ? join(homedir(), path.slice(2)) : path) }
  private samePath(left: string, right: string): boolean {
    const canonical = (path: string) => { try { return realpathSync(this.expand(path)) } catch { return this.expand(path) } }
    return canonical(left) === canonical(right)
  }
  private fleet(): FleetBot[] {
    try {
      const parsed = JSON.parse(readText(process.env.FLEET_CONFIG ? this.expand(process.env.FLEET_CONFIG) : join(this.fleetHome, 'fleet.json'))) as { bots?: unknown }
      if (!Array.isArray(parsed.bots)) return []
      const keys = new Set<string>()
      return parsed.bots.filter((value): value is FleetBot => {
        if (!value || typeof value !== 'object') return false
        const b = value as FleetBot
        if (typeof b.key !== 'string' || !KEY_RE.test(b.key) || keys.has(b.key) || typeof b.name !== 'string' || !b.name.trim() || /[\x00-\x1f]/.test(b.name) || typeof b.state_dir !== 'string' || typeof b.workdir !== 'string' || !b.state_dir || !b.workdir) return false
        keys.add(b.key)
        return true
      }).map(b => ({ ...b, state_dir: this.expand(b.state_dir), workdir: this.expand(b.workdir) }))
    } catch { return [] }
  }
  private self(): FleetBot | undefined { return this.fleet().find(b => this.samePath(b.state_dir, this.deps.stateDir)) }
  private ownerPid(): number | null {
    const value = typeof this.deps.ownerPid === 'function' ? this.deps.ownerPid() : this.deps.ownerPid === undefined ? findOwningClaudePid() : this.deps.ownerPid
    return typeof value === 'number' && Number.isInteger(value) && value > 1 ? value : null
  }
  private engine(name: string): string { return join(this.fleetHome, 'bin', name) }
  private async send(chatId: string, text: string): Promise<void> { await this.deps.bot.api.sendMessage(chatId, text).catch(() => {}) }
  private launch(name: string, args: string[], senderId: string): boolean {
    try {
      const path = this.engine(name)
      if (!executable(path)) throw new Error('missing engine')
      const logs = join(this.deps.stateDir, 'logs')
      mkdirSync(logs, { recursive: true, mode: 0o700 })
      // No asynchronous boundary between this disk-fresh check and spawning.
      if (!this.diskAllows(senderId)) { this.logEvent('SPAWN-DENIED', { engine: name }); return false }
      this.detached(path, args, join(logs, `${name.replace(/^fleet-/, '')}.log`))
      this.logEvent('SPAWN', { engine: name, ok: 1 })
      return true
    } catch { this.logEvent('SPAWN', { engine: name, ok: 0 }); return false }
  }
  private background(work: Promise<unknown>): void {
    const task = work.catch(() => { this.logEvent('HANDLER-ERROR') })
    this.tasks.add(task)
    void task.finally(() => this.tasks.delete(task))
  }

  private diskAllows(senderId: string): boolean {
    try {
      const access = JSON.parse(readText(join(this.deps.stateDir, 'access.json'))) as { dmPolicy?: unknown; allowFrom?: unknown }
      return access?.dmPolicy !== 'disabled' && Array.isArray(access?.allowFrom) && access.allowFrom.every(id => typeof id === 'string') && access.allowFrom.includes(senderId)
    } catch { return false }
  }
  allowedCallback(ctx: Context): boolean {
    return !!ctx.from && !ctx.from.is_bot && ctx.callbackQuery?.message?.chat.type === 'private' && String(ctx.callbackQuery.message.chat.id) === String(ctx.from.id) && this.diskAllows(String(ctx.from.id))
  }

  registerHandlers(): void {
    if (this.registered) return
    const composer = new Composer<Context>()
    composer.on('callback_query:data', async (ctx, next) => {
      if (this.disposed) return next()
      if (!await this.handleCallback(ctx)) return next()
    })
    composer.on('message_reaction', async ctx => {
      if (this.disposed) return
      try {
        const reaction = ctx.messageReaction
        const user = reaction.user
        if (reaction.chat.type !== 'private' || !user || user.is_bot || !this.diskAllows(String(user.id))) return
        for (const emoji of addedReactionEmoji(reaction.old_reaction, reaction.new_reaction)) {
          const phrase = this.ux.reactions.find(item => item.emoji === emoji)?.phrase
          if (!phrase) continue
          this.logEvent('REACTION', { emoji })
          this.enqueue(String(reaction.chat.id), () => this.forwardInbound({ chat_id: String(reaction.chat.id), text: phrase, from: user, ts: new Date(this.now()).toISOString(), via: 'reaction', tapped_message_id: String(reaction.message_id), reaction: emoji }))
        }
      } catch { this.logEvent('HANDLER-ERROR') }
    })
    // Commit registration only after the entire local middleware is constructed.
    this.deps.bot.use(composer.middleware())
    this.registered = true
  }

  private callbackKey(ctx: Context): string { return `${ctx.callbackQuery?.message?.chat.id}:${ctx.callbackQuery?.message?.message_id}` }
  private keyboard(ctx: Context): InlineKeyboardMarkup | undefined {
    const key = this.callbackKey(ctx)
    if (this.keyboards.has(key)) return this.keyboards.get(key) ?? undefined
    const msg = ctx.callbackQuery?.message
    return msg && 'reply_markup' in msg ? msg.reply_markup : undefined
  }
  private callbackExists(ctx: Context, data: string): boolean {
    const effective = this.keyboard(ctx)?.inline_keyboard.flat().find(cell => 'callback_data' in cell && cell.callback_data === data)
    const msg = ctx.callbackQuery?.message
    const supplied = msg && 'reply_markup' in msg ? msg.reply_markup?.inline_keyboard.flat().find(cell => 'callback_data' in cell && cell.callback_data === data) : undefined
    return !!effective && !!supplied && effective.text === supplied.text
  }
  private async toast(ctx: Context, text?: string): Promise<void> { await ctx.answerCallbackQuery(text ? { text: text.slice(0, 200) } : {}).catch(() => {}) }
  private async editChoice(ctx: Context, text: string, markup?: InlineKeyboard): Promise<boolean> {
    const msg = ctx.callbackQuery?.message
    const entities = msg && 'entities' in msg ? msg.entities : undefined
    let edited = false
    if (entities?.length) edited = await ctx.editMessageText(text, { entities, ...(markup ? { reply_markup: markup } : {}) }).then(() => true).catch(() => false)
    if (!edited) edited = await ctx.editMessageText(text, markup ? { reply_markup: markup } : {}).then(() => true).catch(() => false)
    return edited
  }
  private original(ctx: Context): string { const msg = ctx.callbackQuery?.message; return msg && 'text' in msg ? msg.text ?? '' : '' }
  private consume(ctx: Context): void { this.keyboards.set(this.callbackKey(ctx), null) }
  private async closePicker(ctx: Context, kind: string, text: string): Promise<void> {
    this.logEvent('CLOSE-ATTEMPT', { kind })
    await this.toast(ctx)
    const closed = await ctx.editMessageText(text).then(() => true).catch(() => false)
    if (closed) { this.consume(ctx); this.logEvent('CLOSE', { kind }) }
    else { this.logEvent('CLOSE-FAILED', { kind }); await this.toast(ctx, 'Could not close that picker - try again.') }
  }

  async handleCallback(ctx: Context): Promise<boolean> {
    const data = ctx.callbackQuery?.data ?? ''
    const qr = QUICK_RE.exec(data), eff = EFFORT_RE.exec(data), md = MODEL_RE.exec(data), me = MODEL_EFFORT_RE.exec(data), ho = HANDOFF_RE.exec(data), rs = RESUME_RE.exec(data)
    const mail = /^mail:[a-f0-9]{24}:(prepare|confirm|cancel)$/.test(data)
    if (!(qr || eff || md || me || ho || rs || data === 'me:close' || data === 'rsx:refresh' || data === 'rsx:close' || mail)) return false
    if (!ctx.from || !this.diskAllows(String(ctx.from.id))) { await this.toast(ctx, 'Not authorized.'); return true }
    if (ctx.callbackQuery?.message?.chat.type !== 'private') { await this.toast(ctx, 'Only works in a direct message.'); return true }
    if (!this.allowedCallback(ctx)) { await this.toast(ctx, 'Not authorized.'); return true }
    const key = this.callbackKey(ctx)
    // Claim before awaiting Telegram; overlapping/replayed callbacks cannot both act.
    if (this.pending.has(key)) { await this.toast(ctx, 'Already handling that choice.'); return true }
    if (!this.callbackExists(ctx, data)) { await this.toast(ctx, 'Already answered.'); return true }
    this.pending.add(key)
    this.logEvent('TAP', { family: data.split(':')[0]!, cell: qr ? `${qr[1]}:${qr[2]}` : data.slice(data.indexOf(':') + 1) })
    try {
      if (mail) { await this.email.handleCallback(ctx); return true }
      if (qr) { await this.handleQuickReply(ctx, Number(qr[1]), Number(qr[2])); return true }
      const chat = String(ctx.callbackQuery!.message!.chat.id)
      if (md) { await this.handleModelPick(ctx, md[1]!); return true }
      if (data === 'rsx:refresh') { await this.handleResumeRefresh(ctx); return true }
      if (rs) { await this.handleResumeOpen(ctx, rs[1]!); return true }
      if (data === 'rsx:close') { await this.handleResumeClose(ctx); return true }
      if (eff) {
        if (eff[1] === 'close') await this.closePicker(ctx, this.original(ctx).includes('Tap a model') ? 'model' : 'effort', 'Effort picker closed.')
        else { this.consume(ctx); this.logEvent('PICK', { kind: 'effort' }); await this.toast(ctx, `${eff[1]} effort`); await ctx.editMessageText(`Picked: ${eff[1]} effort.`).catch(() => {}); await this.handleEngine('model', chat, eff[1]!, String(ctx.from.id)) }
      } else if (me) {
        this.consume(ctx)
        this.logEvent('PICK', { kind: 'stage2' })
        await this.toast(ctx, `${MODEL_LABELS[me[1]!]} at ${me[2]}`)
        await ctx.editMessageText(`Picked: ${MODEL_LABELS[me[1]!]} at ${me[2]} effort.`).catch(() => {})
        await this.handleEngine('model', chat, `${me[1]} ${me[2]}`, String(ctx.from.id))
      } else if (data === 'me:close') {
        await this.closePicker(ctx, 'stage2', 'Model picker closed.')
      } else if (ho) {
        if (data === 'ho:pick') { this.consume(ctx); await this.handleHandoffPicker(ctx) }
        else if (data === 'ho:cancel') await this.closePicker(ctx, 'handoff', 'Handoff cancelled.')
        else { this.consume(ctx); await this.handleHandoffPick(ctx, ho[1]!) }
      }
    } catch { this.logEvent('HANDLER-ERROR'); await this.toast(ctx, 'Could not handle that choice - try again.') }
    finally { this.pending.delete(key) }
    return true
  }

  private async handleQuickReply(ctx: Context, row: number, col: number): Promise<void> {
    const cell = this.keyboard(ctx)?.inline_keyboard[row]?.[col]
    const label = cell && typeof cell.text === 'string' ? cell.text.trim() : ''
    if (!label || !('callback_data' in cell!) || cell!.callback_data !== ctx.callbackQuery?.data) { await this.toast(ctx, 'Already answered.'); return }
    const chat = String(ctx.callbackQuery!.message!.chat.id)
    // Consent invalidation is immediate, but a keyboard swap is committed only
    // after Telegram confirms its edit. The pending-message lock prevents races.
    this.email.beforeEdit(chat, ctx.callbackQuery!.message!.message_id)
    const normalized = label.toLowerCase().replace(/^[^\p{L}\p{N}]+/u, '').trim()
    if (normalized === 'send it') {
      const swap = new InlineKeyboard().text('✖ Cancel', 'qr:0:0').text('✅ Confirm send', 'qr:0:1')
      this.logEvent('SENDIT-SWAP-ATTEMPT')
      await this.toast(ctx, 'Confirm?')
      if (!this.allowedCallback(ctx)) { this.logEvent('SENDIT-SWAP-DENIED'); return }
      if (await this.editChoice(ctx, `${this.original(ctx)}\n▸ ${label}`, swap)) {
        this.keyboards.set(this.callbackKey(ctx), swap)
        this.logEvent('SENDIT-SWAP')
      } else { this.logEvent('SENDIT-SWAP-FAILED'); await this.toast(ctx, 'Could not update those choices - try again.') }
      return
    }
    this.consume(ctx)
    await this.toast(ctx, label)
    await this.editChoice(ctx, `${this.original(ctx)}\n▸ ${label}`)
    if (label.toLowerCase() === 'compact now') { await this.handleEngine('compact', chat, '', String(ctx.from!.id)); return }
    this.inject(ctx, label, 'button')
  }

  private markInbound(): void { try { writeFileSync(join(this.deps.stateDir, 'last-inbound'), String(Math.floor(this.now() / 1000)) + '\n') } catch {} }
  private inject(ctx: Context, text: string, via: 'button' | 'email_confirm', extra: { consent_id?: string } = {}): void {
    const msg = ctx.callbackQuery?.message
    if (!msg || !ctx.from || !this.allowedCallback(ctx)) return
    const chat = String(msg.chat.id)
    this.enqueue(chat, () => this.forwardInbound({ chat_id: chat, text, from: ctx.from!, ts: new Date(this.now()).toISOString(), via, tapped_message_id: String(msg.message_id), ...extra }))
  }

  async intercept(ctx: Context, text: string, chatId: string): Promise<boolean> {
    if (ctx.chat?.type !== 'private' || !ctx.from || String(ctx.from.id) !== chatId || !this.diskAllows(String(ctx.from.id))) return false
    const trimmed = text.trim(), lower = trimmed.toLowerCase()
    let command: string | undefined
    let action: (() => Promise<void>) | undefined
    // /fresh deliberately keeps its live exact lowercase form.
    if (trimmed === '/fresh') { command = 'fresh'; action = () => this.handleEngine('fresh', chatId) }
    else if (lower === '/model' || lower.startsWith('/model ')) { command = 'model'; action = () => lower === '/model' ? this.handleModelPicker(chatId) : this.handleEngine('model', chatId, trimmed.slice(6).trim()) }
    else if (lower === '/effort' || lower.startsWith('/effort ')) {
      command = 'effort'
      const arg = trimmed.slice(7).trim().replace(/\s+/g, ' '), level = arg.toLowerCase()
      action = () => isEffort(level) ? this.handleEngine('model', chatId, level) : this.handleEffort(chatId, arg ? `Didn't recognize "${arg.slice(0, 40)}". Levels: low, medium, high, xhigh, max.` : '')
    } else if (lower === '/opus' || lower === '/fable') { command = lower.slice(1); action = () => this.handleEngine('model', chatId, lower.slice(1)) }
    else if (['/esc', '/update', '/context', '/usage'].includes(lower)) { command = lower.slice(1); action = () => this.handleEngine(command!, chatId) }
    else if (/^\/compact(?:\s+([\s\S]*))?$/i.test(trimmed)) { command = 'compact'; action = () => this.handleEngine('compact', chatId, trimmed.slice(8).replace(/[\x00-\x1f\x7f]/g, ' ').replace(/\s+/g, ' ').trim().slice(0, 300)) }
    else if (lower === '/key') { command = 'key'; action = async () => {
      this.logEvent('KEY-ATTEMPT')
      const sent = await this.deps.bot.api.sendMessage(chatId, keyText(this.ux)).then(() => true).catch(() => false)
      this.logEvent(sent ? 'KEY' : 'KEY-FAILED')
    } }
    else if (lower === '/inbox' && !this.email.isConfigured()) { command = 'inbox'; action = () => this.send(chatId, 'Inbox is not connected. On your Mac run: fleet-email setup') }
    // /resume and configured /inbox are forwarded to their existing hook/skill.
    if (!action) return false
    this.logEvent('INTERCEPT', { cmd: command! })
    this.background(action())
    return true
  }

  private async handleEngine(kind: string, chatId: string, argument = '', senderId = chatId): Promise<void> {
    if (!this.diskAllows(senderId)) { this.logEvent('SPAWN-DENIED', { engine: `fleet-${kind}` }); return }
    const needsPid = ['fresh', 'esc', 'compact', 'update'].includes(kind)
    const self = this.self(), pid = needsPid ? this.ownerPid() : null
    if (!self || (needsPid && !pid)) { await this.send(chatId, `Could not run /${kind} automatically - use the terminal.`); return }
    const args = kind === 'model' ? [self.workdir, chatId, argument]
      : kind === 'fresh' ? [String(pid), self.workdir, 'fresh-reset']
      : kind === 'compact' ? [String(pid), self.workdir, ...(argument ? [argument] : [])]
      : needsPid ? [String(pid), self.workdir, chatId]
      : [self.workdir, chatId]
    if (kind === 'fresh') await this.send(chatId, 'Starting fresh session... back in ~5s.')
    if (kind === 'esc') await this.send(chatId, '⚡ Escape sent - jumping to your latest message.')
    if (!this.launch(`fleet-${kind}`, args, senderId)) await this.send(chatId, `/${kind} failed to launch - use the terminal.`)
  }

  private readModelState(): { model: string; effort: Effort } | null {
    const lines = readText(join(this.deps.stateDir, 'model-state')).split('\n')
    const pick = (key: string) => lines.find(line => line.startsWith(`${key}=`))?.slice(key.length + 1).replace(/\s+/g, '') ?? ''
    const model = pick('MODEL'), effort = pick('EFFORT')
    return (MODEL_LABELS[model] || /^claude-[a-zA-Z0-9._-]+$/.test(model)) && isEffort(effort) ? { model, effort } : null
  }
  private effortKeyboard(prefix: string, close: string): InlineKeyboard {
    const current = this.readModelState(), keyboard = new InlineKeyboard()
    EFFORTS.forEach((effort, index) => { if (index === 3) keyboard.row(); keyboard.text(current?.effort === effort ? `● ${effort}` : effort, `${prefix}:${effort}`) })
    return keyboard.row().text('Close', close)
  }
  private async handleEffort(chatId: string, lead: string): Promise<void> {
    const current = this.readModelState()
    const body = current ? `This chat is on ${MODEL_LABELS[current.model] ?? current.model} at ${current.effort} effort. Pick a level:` : 'Pick an effort level for this chat:'
    this.logEvent('PICKER-ATTEMPT', { kind: 'effort' })
    const sent = await this.deps.bot.api.sendMessage(chatId, lead ? `${lead}\n\n${body}` : body, { reply_markup: this.effortKeyboard('eff', 'eff:close') }).then(() => true).catch(() => false)
    this.logEvent(sent ? 'PICKER' : 'PICKER-FAILED', { kind: 'effort' })
  }
  private async handleModelPicker(chatId: string): Promise<void> {
    const current = this.readModelState(), keyboard = new InlineKeyboard()
    for (const word of MODELS) keyboard.text(current?.model === word ? `● ${MODEL_LABELS[word]}` : MODEL_LABELS[word]!, `md:${word}`)
    keyboard.inline_keyboard.push(...this.effortKeyboard('eff', 'eff:close').inline_keyboard)
    const lead = current ? `This chat is on ${MODEL_LABELS[current.model] ?? current.model} at ${current.effort} effort.\n` : ''
    this.logEvent('PICKER-ATTEMPT', { kind: 'model' })
    const sent = await this.deps.bot.api.sendMessage(chatId, lead + 'Tap a model and it asks for the effort next, or tap an effort level to keep this model:', { reply_markup: keyboard }).then(() => true).catch(() => false)
    this.logEvent(sent ? 'PICKER' : 'PICKER-FAILED', { kind: 'model' })
  }
  private async handleModelPick(ctx: Context, word: string): Promise<void> {
    const keyboard = this.effortKeyboard(`me:${word}`, 'me:close')
    this.logEvent('PICKER-ATTEMPT', { kind: 'stage2' })
    await this.toast(ctx, MODEL_LABELS[word])
    if (!this.allowedCallback(ctx)) { this.logEvent('PICKER-DENIED', { kind: 'stage2' }); return }
    const edited = await ctx.editMessageText(`Picked: ${MODEL_LABELS[word]}. Now pick an effort level:`, { reply_markup: keyboard }).then(() => true).catch(() => false)
    if (edited) { this.keyboards.set(this.callbackKey(ctx), keyboard); this.logEvent('PICKER', { kind: 'stage2' }) }
    else { this.logEvent('PICKER-FAILED', { kind: 'stage2' }); await this.toast(ctx, 'Could not update that picker - try again.') }
  }

  private async readFleetState(): Promise<FleetRow[] | null> {
    let stdout = ''
    try { stdout = (await this.runOutput(this.engine('fleet-state'), ['--json'], { timeout: 8000, maxBuffer: 256 * 1024 })).stdout }
    catch (error) { stdout = String((error as { stdout?: unknown } | null)?.stdout ?? '') }
    // A nonzero helper may still return useful up/busy status without context.
    try { const parsed: unknown = JSON.parse(stdout); return Array.isArray(parsed) ? parsed.filter((row): row is FleetRow => row !== null && typeof row === 'object') : null } catch { return null }
  }
  private handoffRowFor(bot: FleetBot, rows: FleetRow[] | null): FleetRow | undefined {
    return rows?.find(row => row.key === bot.key) ?? rows?.find(row => typeof row.state_dir === 'string' && this.samePath(row.state_dir, bot.state_dir)) ?? rows?.find(row => row.state_name === basename(bot.state_dir)) ?? rows?.find(row => row.name === bot.name)
  }
  private handoffLabel(bot: FleetBot, row: FleetRow | undefined): string {
    const name = bot.name
    if (!row || typeof row.up !== 'boolean') return name.slice(0, 32)
    if (!row.up) return `${name} · down`.slice(0, 32)
    const pct = typeof row.context_pct === 'number' && Number.isFinite(row.context_pct) ? `${Math.round(row.context_pct)}%` : '??'
    return `${name} · ${row.busy === true ? 'busy' : 'idle'} · ${pct}`.slice(0, 32)
  }
  private otherBot(bot: FleetBot): boolean {
    const self = this.self()
    return bot.key !== self?.key && !this.samePath(bot.state_dir, this.deps.stateDir) && (!self || !this.samePath(bot.workdir, self.workdir))
  }
  private async handleHandoffPicker(ctx: Context): Promise<void> {
    await this.toast(ctx, 'Hand off')
    await this.editChoice(ctx, `${this.original(ctx)}\n▸ Hand off`)
    const rows = await this.readFleetState()
    const others = this.fleet().filter(bot => this.otherBot(bot) && !(this.handoffRowFor(bot, rows)?.state_dir && this.samePath(this.handoffRowFor(bot, rows)!.state_dir!, this.deps.stateDir)))
    const keyboard = new InlineKeyboard()
    for (const bot of others) keyboard.text(this.handoffLabel(bot, this.handoffRowFor(bot, rows)), `ho:${['pick', 'cancel'].includes(bot.key) ? 'bot:' : ''}${bot.key}`).row()
    keyboard.text('Cancel', 'ho:cancel')
    this.logEvent('PICKER-ATTEMPT', { kind: 'handoff' })
    if (!this.allowedCallback(ctx)) { this.logEvent('PICKER-DENIED', { kind: 'handoff' }); return }
    const sent = await this.deps.bot.api.sendMessage(String(ctx.callbackQuery!.message!.chat.id), others.length ? 'Hand off to which bot?' : 'No other bots are configured.', { reply_markup: keyboard }).then(() => true).catch(() => false)
    this.logEvent(sent ? 'PICKER' : 'PICKER-FAILED', { kind: 'handoff' })
    if (sent) this.logEvent('HANDOFF-PICKER', { offered: others.map(bot => bot.key).join(',') || 'none' })
  }
  private async handleHandoffPick(ctx: Context, key: string): Promise<void> {
    const bot = this.fleet().find(bot => bot.key === key)
    if (!bot || !this.otherBot(bot)) { await this.toast(ctx, 'That one is not available.'); return }
    this.logEvent('PICK', { kind: 'handoff' })
    await this.toast(ctx, bot.name)
    await ctx.editMessageText(`Picked: ${bot.name}.`).catch(() => {})
    this.inject(ctx, `Hand off to ${bot.name}`, 'button')
  }

  private async renderResumePicker(): Promise<{ text: string; reply_markup?: InlineKeyboardMarkup; count: number; labels: number } | null> {
    const current = readText(join(this.deps.stateDir, 'session.id')).trim(), self = this.self()
    if (!self) return null
    const args = ['render', '--limit', '20', ...(UUID_RE.test(current) ? ['--exclude-uuid', current] : []), '--bot', self.key, '--style', 'inline']
    try {
      const { stdout } = await this.runOutput(this.engine('fleet-sessions'), args, { timeout: 8000, maxBuffer: 256 * 1024 })
      const parsed = JSON.parse(stdout) as { text?: unknown; reply_markup?: unknown; count?: unknown; labels?: unknown }
      if (typeof parsed?.text !== 'string' || !parsed.text || parsed.text.length > 4096) return null
      const markup = parsed.reply_markup as InlineKeyboardMarkup | undefined
      if (markup && (!Array.isArray(markup.inline_keyboard) || !markup.inline_keyboard.every(row => Array.isArray(row) && row.every(cell => typeof cell?.text === 'string' && 'callback_data' in cell && typeof cell.callback_data === 'string' && (RESUME_RE.test(cell.callback_data) || ['rsx:refresh', 'rsx:close'].includes(cell.callback_data)))))) return null
      return { text: parsed.text, reply_markup: markup, count: typeof parsed.count === 'number' ? parsed.count : 0, labels: typeof parsed.labels === 'number' ? parsed.labels : 0 }
    } catch { return null }
  }
  private async handleResumeOpen(ctx: Context, uuid: string): Promise<void> {
    if (this.resumeOpenInFlight) { await this.toast(ctx, 'Already reopening - one moment.'); return }
    this.resumeOpenInFlight = true
    const self = this.self(), chat = String(ctx.callbackQuery!.message!.chat.id)
    if (!self || !executable(this.engine('fleet-resume-open'))) {
      this.resumeOpenInFlight = false
      await this.toast(ctx, self ? 'Resume helper is missing - reopen from the terminal.' : 'Could not tell which worktree this bot runs in - reopen from the terminal.')
      return
    }
    this.consume(ctx)
    this.logEvent('PICK', { kind: 'resume' })
    await this.toast(ctx, 'Reopening that chat...')
    await ctx.editMessageText(`${this.original(ctx)}\n\nReopening...`).catch(() => {})
    if (!this.launch('fleet-resume-open', [uuid, self.workdir, chat], String(ctx.from!.id))) {
      this.resumeOpenInFlight = false
      await this.send(chat, 'Reopen failed to launch - resume from the terminal.')
      return
    }
    const release = () => {
      if (this.disposed || !this.resumeOpenInFlight) return
      this.resumeOpenInFlight = false
      this.logEvent('RESUME-RELEASE')
      this.background(this.send(chat, 'That reopen did not go through - nothing was restarted. Send /resume to try again.'))
    }
    if (this.deps.schedule) this.deps.schedule(release, 60_000).unref?.()
    else {
      const timer = setTimeout(() => { this.timers.delete(timer); release() }, 60_000)
      this.timers.add(timer)
      timer.unref()
    }
  }
  private async handleResumeRefresh(ctx: Context): Promise<void> {
    this.logEvent('PICKER-ATTEMPT', { kind: 'resume' })
    const picker = await this.renderResumePicker()
    if (!picker) { this.logEvent('PICKER-FAILED', { kind: 'resume' }); await this.toast(ctx, 'Could not refresh the list - try again.'); return }
    if (!this.allowedCallback(ctx)) { this.logEvent('PICKER-DENIED', { kind: 'resume' }); return }
    try {
      await ctx.editMessageText(picker.text, { parse_mode: 'HTML', reply_markup: picker.reply_markup })
      this.keyboards.set(this.callbackKey(ctx), picker.reply_markup ?? null)
      this.logEvent('PICKER', { kind: 'resume', count: picker.count, labels: picker.labels })
      await this.toast(ctx, 'Updated.')
    } catch (error) {
      const unchanged = error instanceof GrammyError && /message is not modified/i.test(error.description)
      this.logEvent(unchanged ? 'PICKER-UNCHANGED' : 'PICKER-FAILED', { kind: 'resume' })
      await this.toast(ctx, unchanged ? 'Nothing new' : 'Could not update the list.')
    }
  }
  private async handleResumeClose(ctx: Context): Promise<void> {
    this.logEvent('CLOSE-ATTEMPT', { kind: 'resume' })
    let closed = await ctx.deleteMessage().then(() => true).catch(() => false)
    if (!closed) closed = await ctx.editMessageReplyMarkup().then(() => true).catch(() => false)
    if (closed) this.consume(ctx)
    this.logEvent(closed ? 'CLOSE' : 'CLOSE-FAILED', { kind: 'resume' })
    await this.toast(ctx, closed ? undefined : 'Could not close that one - it is too old to change.')
  }

  private enqueue(chatId: string, work: () => Promise<void>): void {
    if (this.disposed) return
    const next = (this.chains.get(chatId) ?? Promise.resolve()).then(work).catch(() => { this.logEvent('FORWARD-ERROR') })
    this.chains.set(chatId, next)
    void next.then(() => { if (this.chains.get(chatId) === next) this.chains.delete(chatId) })
  }
  async enqueueForward(ctx: Context, text: string, chatId: string, downloadImage?: () => Promise<string | undefined>, attachment?: AttachmentMeta): Promise<void> {
    if (!ctx.from) return
    const from = ctx.from, message = ctx.message, access = this.deps.loadAccess()
    this.logEvent('INBOUND', { kind: attachment?.kind ?? (downloadImage ? 'photo' : 'text') })
    this.enqueue(chatId, () => this.forwardInbound({ chat_id: chatId, text, from, message_id: message?.message_id, ts: new Date((message?.date ?? 0) * 1000).toISOString(), ackReaction: access.ackReaction, downloadImage, attachment }))
  }
  private async downloadToInbox(fileId: string): Promise<{ path: string; size: number }> {
    if (this.deps.downloadFile) return this.deps.downloadFile(fileId)
    const file = await this.deps.bot.api.getFile(fileId)
    if (!file.file_path) throw new Error('No downloadable file')
    const root = validatedApiRoot(this.deps.apiRoot ?? process.env.TELEBOTS_API_ROOT)
    const response = await fetch(`${root}/file/bot${this.deps.token}/${file.file_path}`, { signal: AbortSignal.timeout(60_000), redirect: 'error' })
    if (!response.ok) throw new Error('Download failed')
    const data = Buffer.from(await response.arrayBuffer())
    if (data.length > 20_000_000) throw new Error('Download too large')
    const extension = (file.file_path.split('.').pop() ?? 'bin').replace(/[^a-zA-Z0-9]/g, '') || 'bin'
    const unique = (file.file_unique_id ?? 'dl').replace(/[^a-zA-Z0-9_-]/g, '') || 'dl'
    const inbox = join(this.deps.stateDir, 'inbox')
    mkdirSync(inbox, { recursive: true, mode: 0o700 })
    const path = join(inbox, `${this.now()}-${unique}-${randomBytes(4).toString('hex')}.${extension}`)
    writeFileSync(path, data, { mode: 0o600 })
    return { path, size: data.length }
  }
  private async transcribeInbound(attachment: AttachmentMeta, messageId: number | undefined, fallback: string): Promise<{ content: string; meta: Record<string, string> }> {
    const duration = attachment.duration
    let status: TranscriptStatus = 'failed', content = fallback, path: string | undefined, chars = 0, dlMs = 0, sttMs = 0
    try {
      if (typeof duration !== 'number' || !Number.isFinite(duration) || duration < 0 || duration > 900 || (attachment.size ?? 0) > 20_000_000) status = 'skipped'
      else {
        const dlStart = this.now()
        let timer: ReturnType<typeof setTimeout> | undefined
        try {
          const stalled = new Promise<never>((_, reject) => { timer = setTimeout(() => reject(new Error('download expired')), 60_000) })
          path = (await Promise.race([this.downloadToInbox(attachment.file_id), stalled])).path
        } finally { if (timer) clearTimeout(timer); dlMs = this.now() - dlStart }
        const sttStart = this.now()
        let transcript = ''
        try {
          transcript = (await this.runOutput(this.engine('transcribe-voice'), [path], { timeout: Math.min(120_000, 20_000 + 200 * duration), maxBuffer: 4 * 1024 * 1024 })).stdout.trim()
          status = transcript ? 'ok' : 'empty'
        } catch (error) {
          const err = error as { killed?: unknown; message?: unknown } | null
          status = err?.killed === true || /timed out|timeout/i.test(String(err?.message ?? '')) ? 'timeout' : 'failed'
        } finally { sttMs = this.now() - sttStart }
        chars = status === 'ok' ? transcript.length : 0
        content = voiceBody(status, attachment.kind, duration, transcript, attachment.caption, fallback)
      }
    } catch { status = 'failed'; content = fallback }
    this.logEvent('VOICE', { status })
    try {
      appendFileSync(join(this.deps.stateDir, 'logs', 'voice-transcribe.log'), `${new Date(this.now()).toISOString()} msg=${messageId ?? '?'} kind=${attachment.kind === 'audio' ? 'audio' : 'voice'} dur=${typeof duration === 'number' && Number.isFinite(duration) ? duration : '?'} dl_ms=${dlMs} stt_ms=${sttMs} status=${status} chars=${chars}\n`, { mode: 0o600 })
    } catch {}
    return { content, meta: { transcript: status, ...(typeof duration === 'number' && Number.isFinite(duration) ? { audio_duration_s: String(duration) } : {}), ...(path ? { audio_path: safeMeta(path) } : {}) } }
  }
  private async forwardInbound(params: ForwardParams): Promise<void> {
    const { chat_id, text, from, message_id, ts, ackReaction, downloadImage, attachment, via, tapped_message_id, reaction, consent_id } = params
    const syntheticAllowed = () => !via || (chat_id === String(from.id) && this.diskAllows(String(from.id)))
    if (!syntheticAllowed()) { this.logEvent('FORWARD-DENIED', { kind: via! }); return }
    void this.deps.bot.api.sendChatAction(chat_id, 'typing').catch(() => {})
    if (ackReaction && message_id != null) void this.deps.bot.api.setMessageReaction(chat_id, message_id, [{ type: 'emoji', emoji: ackReaction as ReactionTypeEmoji['emoji'] }]).catch(() => {})
    const imagePath = downloadImage ? await downloadImage() : undefined
    const voice = attachment && ['voice', 'audio'].includes(attachment.kind) && !existsSync(join(this.deps.stateDir, 'autotranscribe.off')) ? await this.transcribeInbound(attachment, message_id, text) : undefined
    // The item may have waited behind voice or another delivery. Re-read access
    // at the actual MCP handoff, with no await between authorization and the call.
    if (!syntheticAllowed()) { this.logEvent('FORWARD-DENIED', { kind: via! }); return }
    if (via) this.markInbound()
    await this.deps.mcp.notification({ method: 'notifications/claude/channel', params: { content: voice?.content ?? text, meta: {
      chat_id, ...(message_id != null ? { message_id: String(message_id) } : {}), user: safeMeta(from.username ?? String(from.id)), user_id: String(from.id), ts,
      ...(via ? { via } : {}), ...(tapped_message_id ? { tapped_message_id } : {}), ...(reaction ? { reaction } : {}), ...(consent_id ? { consent_id } : {}), ...(imagePath ? { image_path: safeMeta(imagePath) } : {}),
      ...(attachment ? { attachment_kind: safeMeta(attachment.kind), attachment_file_id: safeMeta(attachment.file_id), ...(attachment.size != null ? { attachment_size: String(attachment.size) } : {}), ...(attachment.mime ? { attachment_mime: safeMeta(attachment.mime) } : {}), ...(attachment.name ? { attachment_name: safeMeta(attachment.name) } : {}) } : {}),
      ...(voice?.meta ?? {}),
    } } })
  }
  dispose(): void { this.disposed = true; this.email.dispose(); for (const timer of this.timers) clearTimeout(timer); this.timers.clear() }
  async drain(): Promise<void> {
    while (this.chains.size || this.tasks.size) await Promise.all([...this.chains.values(), ...this.tasks])
  }
}
