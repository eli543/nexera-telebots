import { createHash, createHmac, randomBytes } from 'node:crypto'
import { closeSync, constants, existsSync, fstatSync, fsyncSync, lstatSync, mkdirSync, openSync, readFileSync, renameSync, unlinkSync, writeFileSync } from 'node:fs'
import { isAbsolute, join } from 'node:path'
import { spawn } from 'node:child_process'
import { createServer, type Server } from 'node:net'
import type { Bot, Context } from 'grammy'
import type { InlineKeyboardMarkup } from 'grammy/types'
import type { CallToolResult, Tool } from '@modelcontextprotocol/sdk/types.js'

export type Email = { to: string[]; cc?: string[]; bcc?: string[]; subject: string; body: string }
export type CanonicalEmail = Required<Email>
export type ConsentPhase = 'preview' | 'confirm' | 'authorized' | 'consumed' | 'cancelled'
export type ConsentRecord = {
  id: string; chat: string; message: number; digest: string; expires: number
  phase: ConsentPhase; email: CanonicalEmail
}

const ID = /^[a-f0-9]{24}$/
const CHAT = /^[1-9][0-9]*$/
const PHASES = new Set<ConsentPhase>(['preview', 'confirm', 'authorized', 'consumed', 'cancelled'])
const TTL = 600_000
const PREVIEW_LIMIT = 3900
const STALE = 'Fresh confirmation for this exact email is required.'
const BACKEND_UNCERTAIN = 'The email backend did not confirm its result. Consent is spent. Do not retry automatically; check the mailbox before requesting new consent.'

function plainObject(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
}

function validUnicode(value: string): boolean {
  // Reject lone UTF-16 surrogates so every adapter can hash identical UTF-8 bytes.
  return !/[\uD800-\uDBFF](?![\uDC00-\uDFFF])|(?<![\uD800-\uDBFF])[\uDC00-\uDFFF]/u.test(value)
}

export function canonicalEmail(value: unknown): CanonicalEmail {
  if (!plainObject(value) || Object.keys(value).some(key => !['to', 'cc', 'bcc', 'subject', 'body'].includes(key))) {
    throw Error('Email must contain only to, cc, bcc, subject and body.')
  }
  const recipients = (field: 'to' | 'cc' | 'bcc'): string[] => {
    const list = value[field] === undefined && field !== 'to' ? [] : value[field]
    if (!Array.isArray(list) || (field === 'to' && !list.length) || list.some(item =>
      typeof item !== 'string' || item.length > 254 ||
      !/^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?\.[A-Za-z]{2,63}$/.test(item))) {
      throw Error('Use explicit email addresses in every recipient field.')
    }
    return [...list] as string[]
  }
  if (typeof value.subject !== 'string' || /[\u0000-\u001f\u007f]/.test(value.subject) ||
      typeof value.body !== 'string' || /[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]/.test(value.body) ||
      !validUnicode(value.subject) || !validUnicode(value.body)) {
    throw Error('Use a plain text subject and body without hidden control characters.')
  }
  // C7 wire contract v1: fixed alphabetical keys, no whitespace, UTF-8 JSON.
  // Omitted cc/bcc become []; preserve recipient order/case and all content.
  // Python equivalent: json.dumps(email, ensure_ascii=False, sort_keys=True,
  // separators=(',', ':')).encode('utf-8'), after materializing cc and bcc.
  return { bcc: recipients('bcc'), body: value.body, cc: recipients('cc'), subject: value.subject, to: recipients('to') }
}

export function canonicalEmailJson(email: unknown): string { return JSON.stringify(canonicalEmail(email)) }
export function emailDigest(email: unknown): string { return createHash('sha256').update(canonicalEmailJson(email), 'utf8').digest('hex') }

export function emailText(value: unknown): string {
  const email = canonicalEmail(value)
  const text = `To: ${email.to.join(', ')}\nCc: ${email.cc.join(', ')}\nBcc: ${email.bcc.join(', ')}\nSubject: ${email.subject}\n\n${email.body}`
  if (text.length > PREVIEW_LIMIT) throw Error('The complete email exceeds the 3900-character preview limit. Shorten it before requesting consent.')
  return text
}

export class ConsentStore {
  readonly path: string
  private readonly lockPath: string
  private active: Record<string, ConsentRecord> = Object.create(null) as Record<string, ConsentRecord>

  constructor(readonly stateDir: string, private readonly now: () => number = Date.now) {
    this.path = join(stateDir, 'consents.json')
    this.lockPath = join(stateDir, 'consents.lock')
  }

  private read(): Record<string, ConsentRecord> {
    if (!existsSync(this.path)) return Object.create(null) as Record<string, ConsentRecord>
    const fd = openSync(this.path, constants.O_RDONLY | constants.O_NOFOLLOW)
    try {
      const info = fstatSync(fd)
      if (!info.isFile() || (info.mode & 0o777) !== 0o600 || info.size > 8 * 1024 * 1024) throw Error('Consent storage is unavailable.')
      const data: unknown = JSON.parse(readFileSync(fd, 'utf8'))
      if (!plainObject(data)) throw Error('Consent storage is unavailable.')
      for (const [id, value] of Object.entries(data)) {
        if (!ID.test(id) || !plainObject(value) || value.id !== id || typeof value.chat !== 'string' || !CHAT.test(value.chat) ||
            !Number.isSafeInteger(value.message) || Number(value.message) <= 0 || typeof value.digest !== 'string' || !/^[a-f0-9]{64}$/.test(value.digest) ||
            typeof value.expires !== 'number' || !Number.isSafeInteger(value.expires) || value.expires <= 0 ||
            !PHASES.has(value.phase as ConsentPhase) || emailDigest(value.email) !== value.digest) throw Error('Consent storage is unavailable.')
        emailText(value.email)
      }
      return data as Record<string, ConsentRecord>
    } finally { closeSync(fd) }
  }

  private transaction<T>(change: (records: Record<string, ConsentRecord>) => T, cancelOldPending = false): T {
    mkdirSync(this.stateDir, { recursive: true, mode: 0o700 })
    // One synchronous transaction also prevents separate pollers from authorizing
    // or consuming the same record concurrently. An orphan lock fails closed.
    const lock = openSync(this.lockPath, 'wx', 0o600)
    let temporary: string | undefined
    try {
      const records = structuredClone(this.active)
      const result = change(records)
      const audit = this.read()
      if (cancelOldPending) {
        for (const record of Object.values(audit)) {
          if (record.phase === 'preview' || record.phase === 'confirm' || record.phase === 'authorized') record.phase = 'cancelled'
        }
      }
      Object.assign(audit, records)
      temporary = `${this.path}.${randomBytes(8).toString('hex')}.tmp`
      const fd = openSync(temporary, 'wx', 0o600)
      try { writeFileSync(fd, JSON.stringify(audit)); fsyncSync(fd) } finally { closeSync(fd) }
      renameSync(temporary, this.path)
      temporary = undefined
      this.active = records
      return structuredClone(result)
    } finally {
      if (temporary) { try { unlinkSync(temporary) } catch { /* No content is logged. */ } }
      closeSync(lock)
      unlinkSync(this.lockPath)
    }
  }

  get(id: string): ConsentRecord | undefined {
    if (!ID.test(id)) return undefined
    const record = this.active[id]
    return record ? structuredClone(record) : undefined
  }

  preview(chat: string, message: number, value: unknown): ConsentRecord {
    const email = canonicalEmail(value)
    emailText(email)
    if (!CHAT.test(chat) || !Number.isSafeInteger(message) || message <= 0) throw Error('A private preview message is required.')
    return this.transaction(records => {
      const id = randomBytes(12).toString('hex')
      const record: ConsentRecord = { id, chat, message, digest: emailDigest(email), expires: this.now() + TTL, phase: 'preview', email }
      records[id] = record
      return record
    })
  }

  check(id: string, chat: string, message: number, displayed: string, phase?: ConsentPhase): ConsentRecord {
    const record = this.get(id)
    if (!record || record.chat !== chat || record.message !== message || record.expires <= this.now() ||
        displayed !== emailText(record.email) || (phase !== undefined && record.phase !== phase)) throw Error(STALE)
    return record
  }

  tap(id: string, action: 'prepare' | 'confirm' | 'cancel', chat: string, message: number, displayed: string): ConsentRecord {
    return this.transaction(records => {
      const record = records[id]
      if (!ID.test(id) || !record || record.chat !== chat || record.message !== message || record.expires <= this.now() ||
          displayed !== emailText(record.email)) throw Error(STALE)
      if (action === 'prepare' && record.phase === 'preview') record.phase = 'confirm'
      else if (action === 'confirm' && record.phase === 'confirm') record.phase = 'authorized'
      else if (action === 'cancel' && ['preview', 'confirm'].includes(record.phase)) record.phase = 'cancelled'
      else throw Error(STALE)
      return record
    })
  }

  consume(id: string, chat: string, email: unknown): ConsentRecord {
    const digest = emailDigest(email)
    return this.transaction(records => {
      const record = records[id]
      if (!ID.test(id) || !record || record.phase !== 'authorized' || record.chat !== chat || record.expires <= this.now() || record.digest !== digest) throw Error(STALE)
      record.phase = 'consumed'
      return record
    })
  }

  invalidatePreview(chat: string, message: number): void {
    if (!existsSync(this.path)) return
    this.transaction(records => {
      for (const record of Object.values(records)) {
        if (record.chat === chat && record.message === message && record.phase !== 'consumed') record.phase = 'cancelled'
      }
    })
  }

  invalidatePending(): void {
    try {
      if (!lstatSync(this.path).isFile()) throw Error('Consent storage is unavailable.')
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code === 'ENOENT') return
      throw error
    }
    this.transaction(() => {}, true)
  }
}

export type EmailBackendPayload = { consent_id: string; bot: string; email: CanonicalEmail; nonce: string; chat: string; message: number; digest: string }
export type EmailServiceOptions = {
  bot: Pick<Bot, 'api'>
  stateDir: string
  fleetHome: string
  botKey: () => string | undefined
  assertAllowedChat: (chatId: string) => void | Promise<void>
  logEvent: (event: string, fields: Record<string, string | number>) => void
  injectConfirm: (ctx: Context, consentId: string) => Promise<void>
  allowedCallback: (ctx: Context) => boolean
  now?: () => number
  runBackend?: (executable: string, op: 'send', payload: EmailBackendPayload) => Promise<unknown>
}

const emailSchema = {
  type: 'object', additionalProperties: false,
  properties: {
    to: { type: 'array', minItems: 1, items: { type: 'string' } },
    cc: { type: 'array', items: { type: 'string' } }, bcc: { type: 'array', items: { type: 'string' } },
    subject: { type: 'string' }, body: { type: 'string' },
  }, required: ['to', 'subject', 'body'],
}

export const emailTools: Tool[] = [
  {
    name: 'email_preview',
    description: 'Show the complete exact To, Cc, Bcc, subject and plain text body in a private chat. Creates no provider draft and sends no email. For an inbox reply, pass the read_receipt from fleet-email read to link checklist evidence. Wait for the owner to tap Send it, then Confirm send. Consent expires after 10 minutes; any edit or controller restart cancels it and requires a fresh preview and both taps. Omitted Cc and Bcc mean empty lists. Preview limit: 3900 characters including headers.',
    inputSchema: { type: 'object', additionalProperties: false, properties: { chat_id: { type: 'string' }, email: emailSchema, read_receipt: { type: 'string' } }, required: ['chat_id', 'email'] },
  },
  {
    name: 'email_send',
    description: 'Use only after an email_confirm event with its consent_id. Provide the identical recipients, subject and body from email_preview. The server checks private chat, exact content, two taps and 10-minute expiry, then spends consent before one backend attempt. Reactions and ordinary Send it buttons never authorize email. Never retry an uncertain result automatically; check the mailbox before asking for new consent.',
    inputSchema: { type: 'object', additionalProperties: false, properties: { chat_id: { type: 'string' }, consent_id: { type: 'string' }, email: emailSchema }, required: ['chat_id', 'consent_id', 'email'] },
  },
]

function previewKeyboard(id: string): InlineKeyboardMarkup {
  return { inline_keyboard: [[
    { text: '✉️ Send it', callback_data: `mail:${id}:prepare` },
    { text: '✏️ Edit', callback_data: 'qr:0:1' },
    { text: '⏸ Hold', callback_data: 'qr:0:2' },
  ]] }
}
function confirmKeyboard(id: string): InlineKeyboardMarkup {
  return { inline_keyboard: [[
    { text: '✖ Cancel', callback_data: `mail:${id}:cancel` },
    { text: '✅ Confirm send', callback_data: `mail:${id}:confirm` },
  ]] }
}
function exactKeyboard(actual: unknown, expected: InlineKeyboardMarkup): boolean {
  if (!plainObject(actual) || !Array.isArray(actual.inline_keyboard)) return false
  const rows = actual.inline_keyboard
  return rows.length === expected.inline_keyboard.length && rows.every((row, r) =>
    Array.isArray(row) && row.length === expected.inline_keyboard[r]!.length && row.every((button, c) => {
      const target = expected.inline_keyboard[r]![c]!
      return plainObject(button) && button.text === target.text && 'callback_data' in target && button.callback_data === target.callback_data
    }))
}

export class EmailService {
  readonly tools = emailTools
  readonly consents: ConsentStore
  private readonly busy = new Set<string>()
  private readonly pending = new Map<string, { id: string; chat: string; message: number; digest: string; expires: number }>()
  private readonly replyReads = new Map<string, string>()
  private readonly authority: Server
  private readonly authorityReady: Promise<number>
  private readonly authoritySecret = randomBytes(32).toString('hex')
  private readonly authorityPath: string

  constructor(private readonly options: EmailServiceOptions) {
    this.authorityPath = join(options.stateDir, 'email-authority.json')
    this.consents = new ConsentStore(options.stateDir, options.now)
    // Disk receipts are audit records only. A controller restart begins with no
    // in-memory consent, even if a receipt says authorized or consumed.
    this.consents.invalidatePending()
    // The adapter must ask this process to spend a fresh nonce for this exact
    // digest before contacting SMTP. A local shell in full stance can still
    // reach mail credentials directly; see the documented stance boundary.
    this.authority = createServer(socket => {
      let input = '', answered = false
      socket.setTimeout(3000, () => socket.destroy())
      socket.on('data', (chunk: Buffer) => {
        if (answered) return
        input += chunk.toString('utf8')
        if (Buffer.byteLength(input) > 2048) { socket.destroy(); return }
        if (!input.includes('\n')) return
        answered = true
        let ok = false, mac: string | undefined
        try {
          const value: unknown = JSON.parse(input.slice(0, input.indexOf('\n')))
          if (plainObject(value) && typeof value.nonce === 'string') {
            const pending = this.pending.get(value.nonce)
            const record = pending && this.consents.get(pending.id)
            ok = !!pending && !!record && pending.expires > (this.options.now ?? Date.now)() &&
              value.consent_id === pending.id && value.chat === pending.chat && value.message === pending.message &&
              value.digest === pending.digest && record.phase === 'consumed' && record.chat === pending.chat &&
              record.message === pending.message && record.digest === pending.digest
            if (ok && pending) {
              this.pending.delete(value.nonce)
              mac = createHmac('sha256', Buffer.from(this.authoritySecret, 'hex'))
                .update(`${value.nonce}:${pending.digest}`).digest('hex')
            }
          }
        } catch { /* Invalid proof never authorizes a send. */ }
        socket.end(JSON.stringify(ok ? { ok: true, mac } : { ok: false }) + '\n')
      })
    })
    this.authorityReady = new Promise(resolve => {
      this.authority.once('error', () => resolve(0))
      this.authority.listen(0, '127.0.0.1', () => {
        this.authority.unref()
        const address = this.authority.address()
        const port = address && typeof address !== 'string' ? address.port : 0
        if (!port) { resolve(0); return }
        const temporary = `${this.authorityPath}.${process.pid}.${randomBytes(4).toString('hex')}.tmp`
        try {
          writeFileSync(temporary, JSON.stringify({ port, secret: this.authoritySecret }) + '\n', { flag: 'wx', mode: 0o600 })
          renameSync(temporary, this.authorityPath)
          resolve(port)
        } catch {
          try { unlinkSync(temporary) } catch {}
          this.authority.close()
          resolve(0)
        }
      })
    })
  }

  dispose(): void {
    this.pending.clear(); this.replyReads.clear(); this.authority.close()
    try {
      const current: unknown = JSON.parse(readFileSync(this.authorityPath, 'utf8'))
      if (plainObject(current) && current.secret === this.authoritySecret) unlinkSync(this.authorityPath)
    } catch {}
  }

  private checkedReadReceipt(value: unknown): string | undefined {
    if (value === undefined) return undefined
    if (typeof value !== 'string' || !ID.test(value)) throw Error('A valid inbox read receipt is required.')
    const path = join(this.options.stateDir, 'mail-reads', `${value}.json`)
    const info = lstatSync(path)
    if (!info.isFile() || (info.mode & 0o777) !== 0o600 || info.size > 4096) throw Error('A valid inbox read receipt is required.')
    const receipt: unknown = JSON.parse(readFileSync(path, 'utf8'))
    if (!plainObject(receipt) || receipt.full !== true || typeof receipt.id !== 'string' || !/^\d+$/.test(receipt.id) ||
        typeof receipt.at !== 'number' || receipt.at > Date.now() / 1000 + 60 || receipt.at < Date.now() / 1000 - 86_400) {
      throw Error('A valid inbox read receipt is required.')
    }
    return value
  }

  private event(event: string, fields: Record<string, string | number>): void {
    try { this.options.logEvent(event, fields) } catch { /* Evidence failure cannot turn a send into a retry. */ }
  }

  private backendPath(): string | undefined {
    try {
      const config: unknown = JSON.parse(readFileSync(join(this.options.fleetHome, 'email.json'), 'utf8'))
      if (!plainObject(config) || config.enabled !== true) return undefined
      if (config.backend === 'builtin' ||
          (typeof config.backend === 'string' && isAbsolute(config.backend) && !/[\u0000\r\n]/.test(config.backend))) {
        // Custom adapters are still called through fleet-email so they cannot
        // bypass the one-time poller authorization check.
        return join(this.options.fleetHome, 'bin', 'fleet-email')
      }
    } catch { /* Missing or malformed configuration disables email. */ }
    return undefined
  }

  isConfigured(): boolean { return this.backendPath() !== undefined }

  private diskAllowed(chat: string): boolean {
    if (!CHAT.test(chat)) return false
    try {
      const access: unknown = JSON.parse(readFileSync(join(this.options.stateDir, 'access.json'), 'utf8'))
      return plainObject(access) && access.dmPolicy !== 'disabled' && Array.isArray(access.allowFrom) &&
        access.allowFrom.every(value => typeof value === 'string') && access.allowFrom.includes(chat)
    } catch { return false }
  }

  private async toolAllowed(chat: string): Promise<void> {
    await this.options.assertAllowedChat(chat)
    if (!this.diskAllowed(chat)) throw Error('This private chat is not authorized.')
  }

  beforeEdit(chatId: string, messageId: string | number): void { this.invalidatePreview(chatId, messageId) }
  invalidatePreview(chatId: string, messageId: string | number): void {
    this.consents.invalidatePreview(String(chatId), Number(messageId))
  }

  private async clearKeyboard(chat: string, message: number): Promise<void> {
    await this.options.bot.api.editMessageReplyMarkup(chat, message, { reply_markup: { inline_keyboard: [] } })
  }

  private async toast(ctx: Context, text: string): Promise<void> {
    try { if (ctx.callbackQuery) await this.options.bot.api.answerCallbackQuery(ctx.callbackQuery.id, { text }) } catch { /* Callback may already be too old. */ }
  }

  async handleCallback(ctx: Context): Promise<boolean> {
    const data = ctx.callbackQuery?.data
    if (!data?.startsWith('mail:')) return false
    const match = /^mail:([a-f0-9]{24}):(prepare|confirm|cancel)$/.exec(data)
    if (!match) { await this.toast(ctx, 'This email button is invalid.'); return true }
    const id = match[1]!, action = match[2] as 'prepare' | 'confirm' | 'cancel'
    if (this.busy.has(id)) { await this.toast(ctx, 'This email button is already being handled.'); return true }
    this.busy.add(id)
    try {
      const message = ctx.callbackQuery?.message
      if (!message || !('text' in message) || typeof message.text !== 'string' || message.chat.type !== 'private' ||
          !message.from?.is_bot || message.from.id !== ctx.me.id || message.forward_origin || message.via_bot || !ctx.from || ctx.from.is_bot ||
          String(ctx.from.id) !== String(message.chat.id) || !this.options.allowedCallback(ctx) || !this.diskAllowed(String(message.chat.id))) throw Error(STALE)
      const chat = String(message.chat.id), messageId = message.message_id
      const record = this.consents.check(id, chat, messageId, message.text)
      const expected = record.phase === 'preview' ? previewKeyboard(id) : record.phase === 'confirm' ? confirmKeyboard(id) : undefined
      if (!expected || !exactKeyboard(message.reply_markup, expected)) throw Error(STALE)
      if (action === 'prepare') {
        this.consents.tap(id, action, chat, messageId, message.text)
        try {
          await this.options.bot.api.editMessageReplyMarkup(chat, messageId, { reply_markup: confirmKeyboard(id) })
          if (!this.diskAllowed(chat) || !this.options.allowedCallback(ctx)) throw Error(STALE)
          this.consents.check(id, chat, messageId, message.text, 'confirm')
        } catch {
          this.invalidatePreview(chat, messageId)
          try { await this.clearKeyboard(chat, messageId) } catch { /* Cancelled record remains authoritative. */ }
          throw Error(STALE)
        }
        this.event('SENDIT-SWAP', {})
        await this.toast(ctx, 'Review the email, then confirm send.')
      } else if (action === 'cancel') {
        this.consents.tap(id, action, chat, messageId, message.text)
        try { await this.clearKeyboard(chat, messageId) } catch { /* Cancellation is already durable. */ }
        await this.toast(ctx, 'Email cancelled.')
      } else {
        if (record.phase !== 'confirm') throw Error(STALE)
        try {
          await this.clearKeyboard(chat, messageId)
          if (!this.diskAllowed(chat) || !this.options.allowedCallback(ctx)) throw Error(STALE)
          this.consents.tap(id, action, chat, messageId, message.text)
          await this.options.injectConfirm(ctx, id)
        } catch {
          this.invalidatePreview(chat, messageId)
          throw Error(STALE)
        }
        this.event('EMAIL-CONFIRM', { id })
        await this.toast(ctx, 'Email confirmed once.')
      }
    } catch {
      await this.toast(ctx, 'This email confirmation is unavailable. Request a fresh preview.')
    } finally { this.busy.delete(id) }
    return true
  }

  async callTool(name: string, args: Record<string, unknown>): Promise<CallToolResult | null> {
    if (name !== 'email_preview' && name !== 'email_send') return null
    try {
      if (typeof args.chat_id !== 'string') throw Error('A private chat_id is required.')
      const chat = args.chat_id
      const email = canonicalEmail(args.email)
      await this.toolAllowed(chat)
      const executable = this.backendPath()
      if (!executable) throw Error('Inbox is not connected. On your Mac run: fleet-email setup')
      if (name === 'email_preview') {
        const readReceipt = this.checkedReadReceipt(args.read_receipt)
        const text = emailText(email)
        const message = await this.options.bot.api.sendMessage(chat, text, { reply_markup: { inline_keyboard: [] } })
        let record: ConsentRecord | undefined
        try {
          if (message.chat.type !== 'private' || String(message.chat.id) !== chat || message.text !== text) throw Error('The exact private preview could not be verified.')
          await this.toolAllowed(chat)
          record = this.consents.preview(chat, message.message_id, email)
          await this.options.bot.api.editMessageReplyMarkup(chat, message.message_id, { reply_markup: previewKeyboard(record.id) })
          await this.toolAllowed(chat)
          this.consents.check(record.id, chat, message.message_id, text, 'preview')
        } catch {
          if (record) this.invalidatePreview(chat, message.message_id)
          try { await this.clearKeyboard(chat, message.message_id) } catch { /* A failed preview never authorizes anything. */ }
          throw Error('The complete email preview could not be prepared. No email was sent.')
        }
        this.event('EMAIL-PREVIEW', { id: record.id })
        if (readReceipt) this.replyReads.set(record.id, readReceipt)
        return { content: [{ type: 'text', text: `Exact email shown. Wait for the owner's two taps. Consent ID: ${record.id}` }] }
      }
      const bot = this.options.botKey()
      if (!bot || !/^[a-z0-9][a-z0-9-]{0,30}$/.test(bot)) throw Error('This bot is not configured for email.')
      if (typeof args.consent_id !== 'string') throw Error(STALE)
      const port = await this.authorityReady
      if (!port) throw Error('The email authority is unavailable.')
      // No await between the disk-fresh access check and durable consumption.
      if (!this.diskAllowed(chat)) throw Error('This private chat is not authorized.')
      const record = this.consents.consume(args.consent_id, chat, email)
      const nonce = randomBytes(32).toString('hex')
      this.pending.set(nonce, { id: record.id, chat, message: record.message, digest: record.digest,
        expires: Math.min(record.expires, (this.options.now ?? Date.now)() + 30_000) })
      this.event('EMAIL-SEND', { id: record.id, status: 'attempt' })
      try {
        // The backend spends the nonce through the loopback authority, then
        // creates mail-attempts/<id> with O_EXCL before one SMTP attempt.
        const result = await (this.options.runBackend ?? this.runBackend.bind(this))(executable, 'send',
          { consent_id: record.id, bot, email: record.email, nonce, chat, message: record.message, digest: record.digest })
        if (!plainObject(result) || result.ok !== true) throw Error(BACKEND_UNCERTAIN)
        this.event('EMAIL-SEND', { id: record.id, status: 'sent' })
        const readReceipt = this.replyReads.get(record.id)
        if (readReceipt) this.event('EMAIL-REPLY', { id: record.id, read: readReceipt, status: 'sent' })
        return { content: [{ type: 'text', text: 'The email backend confirmed the send. Consent has been used.' }] }
      } catch {
        this.event('EMAIL-SEND', { id: record.id, status: 'uncertain' })
        throw Error(BACKEND_UNCERTAIN)
      } finally {
        this.pending.delete(nonce)
        this.replyReads.delete(record.id)
      }
    } catch (error) {
      // Never forward adapter stderr/stdout, message text, paths or secrets.
      const text = error instanceof Error && [STALE, BACKEND_UNCERTAIN].includes(error.message) ? error.message :
        error instanceof Error && /^(Email must |Use |A private |This private |The complete |The exact |The email |Inbox is |This bot )/.test(error.message) ? error.message :
        'The email request was refused. No automatic retry is allowed.'
      return { isError: true, content: [{ type: 'text', text }] }
    }
  }

  private async runBackend(executable: string, op: 'send', payload: EmailBackendPayload): Promise<unknown> {
    return await new Promise((resolve, reject) => {
      const env: NodeJS.ProcessEnv = { FLEET_HOME: this.options.fleetHome, TELEGRAM_STATE_DIR: this.options.stateDir }
      for (const key of ['HOME', 'PATH', 'TMPDIR', 'LANG', 'LC_ALL', 'TELEBOTS_TEST', 'TELEBOTS_SANDBOX_ROOT']) if (process.env[key]) env[key] = process.env[key]
      // A soft deadline makes the result uncertain without signalling a child.
      // The adapter must set its own socket deadlines. Never retry after this.
      const child = spawn(executable, [op], { env, stdio: ['pipe', 'pipe', 'ignore'] })
      let settled = false, output = ''
      const finish = (error?: Error, result?: unknown) => {
        if (settled) return
        settled = true
        clearTimeout(timer)
        if (error) reject(error); else resolve(result)
      }
      const timer = setTimeout(() => finish(Error(BACKEND_UNCERTAIN)), 30_000)
      child.stdout.on('data', (chunk: Buffer) => {
        if (settled) return
        output += chunk.toString('utf8')
        if (Buffer.byteLength(output) > 65_536) finish(Error(BACKEND_UNCERTAIN))
      })
      child.on('error', () => finish(Error(BACKEND_UNCERTAIN)))
      child.stdin.on('error', () => finish(Error(BACKEND_UNCERTAIN)))
      child.on('close', code => {
        if (code !== 0) return finish(Error(BACKEND_UNCERTAIN))
        try { finish(undefined, JSON.parse(output)) } catch { finish(Error(BACKEND_UNCERTAIN)) }
      })
      child.stdin.end(JSON.stringify(payload))
    })
  }
}
