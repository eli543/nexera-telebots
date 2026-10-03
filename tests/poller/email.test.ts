import { afterEach, beforeEach, describe, expect, setDefaultTimeout, test } from 'bun:test'
import { Bot, Context } from 'grammy'
import { chmodSync, existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, statSync, writeFileSync } from 'node:fs'
import { join, resolve, sep } from 'node:path'
import { connect } from 'node:net'
import { canonicalEmailJson, ConsentStore, EmailService, emailDigest, emailText, emailTools, type Email, type EmailBackendPayload, type EmailServiceOptions } from '../../poller/email.ts'

// Keep failure cleanup in our code; Bun timeout cleanup sends process signals.
setDefaultTimeout(0)

const sandbox = process.env.TELEBOTS_SANDBOX_ROOT
if (!sandbox || process.env.TELEGRAM_BOT_TOKEN || ![process.env.HOME, process.env.TMPDIR, process.env.FLEET_HOME, process.env.TELEGRAM_STATE_DIR]
  .every(value => value && resolve(value).startsWith(resolve(sandbox) + sep))) throw Error('Run email tests only through the scrubbed sandbox wrapper.')

const fixture: Email = {
  to: ['demo@example.invalid', 'second@example.invalid'], cc: ['copy@example.invalid'], bcc: ['blind@example.invalid'],
  subject: 'DEMO ONLY', body: 'Offline consent fixture. Never send.',
}
const botInfo = {
  id: 900, is_bot: true as const, first_name: 'DEMO', username: 'demo_bot', can_join_groups: false,
  can_read_all_group_messages: false, supports_inline_queries: false, can_connect_to_business: false,
  has_main_web_app: false, has_topics_enabled: false, allows_users_to_create_topics: false,
}
let root: string, state: string, fleet: string, now: number, store: ConsentStore

beforeEach(() => {
  root = mkdtempSync(join(sandbox!, 'email-'))
  state = join(root, 'state'); fleet = join(root, 'fleet')
  mkdirSync(state, { mode: 0o700 }); mkdirSync(fleet, { mode: 0o700 })
  writeFileSync(join(state, 'access.json'), JSON.stringify({ dmPolicy: 'allowlist', allowFrom: ['1', '2'] }), { mode: 0o600 })
  writeFileSync(join(fleet, 'email.json'), JSON.stringify({ enabled: true, backend: 'builtin' }), { mode: 0o600 })
  now = 10_000
  store = new ConsentStore(state, () => now)
})
afterEach(() => { rmSync(root, { recursive: true, force: true }) })

function authorized(email: Email = fixture) {
  const record = store.preview('1', 42, email)
  store.tap(record.id, 'prepare', '1', 42, emailText(email))
  store.tap(record.id, 'confirm', '1', 42, emailText(email))
  return record
}

function harness(overrides: Partial<EmailServiceOptions> = {}) {
  const bot = new Bot('0:offline-fixture', { botInfo })
  const calls: Array<{ method: string; payload: any }> = []
  const messages = new Map<number, any>()
  const injections: Array<{ ctx: Context; id: string }> = []
  const sends: Array<{ executable: string; op: string; payload: EmailBackendPayload }> = []
  const events: Array<{ event: string; fields: Record<string, string | number> }> = []
  const failures = new Set<string>()
  let onCall: ((method: string, payload: any) => Promise<void>) | undefined
  bot.api.config.use(async (_previous, method, payload) => {
    calls.push({ method, payload: structuredClone(payload) })
    if (onCall) await onCall(method, payload)
    if (failures.has(method)) throw Error('DEMO private backend detail')
    const p = payload as any
    if (method === 'sendMessage') {
      const message = { message_id: messages.size + 1, date: Math.floor(now / 1000), from: botInfo, chat: { id: Number(p.chat_id), type: 'private' }, text: p.text, reply_markup: p.reply_markup }
      messages.set(message.message_id, structuredClone(message))
      return { ok: true, result: message } as any
    }
    if (method === 'editMessageReplyMarkup') {
      const message = messages.get(p.message_id)
      if (!message) throw Error('DEMO missing message')
      message.reply_markup = structuredClone(p.reply_markup)
      return { ok: true, result: structuredClone(message) } as any
    }
    if (method === 'answerCallbackQuery') return { ok: true, result: true } as any
    throw Error('An unexpected Telegram method was attempted.')
  })
  const options: EmailServiceOptions = {
    bot, stateDir: state, fleetHome: fleet, botKey: () => 'main', assertAllowedChat: () => {}, now: () => now,
    allowedCallback: () => true, logEvent: (event, fields) => { events.push({ event, fields }) },
    injectConfirm: async (ctx, id) => { injections.push({ ctx, id }) },
    runBackend: async (executable, op, payload) => {
      expect(JSON.parse(readFileSync(join(state, 'consents.json'), 'utf8'))[payload.consent_id]?.phase).toBe('consumed')
      expect(existsSync(join(state, 'mail-attempts', payload.consent_id))).toBeFalse()
      sends.push({ executable, op, payload })
      return { ok: true }
    }, ...overrides,
  }
  const service = new EmailService(options)
  store = service.consents
  function context(data: string, message: any, sender = 1): Context {
    return new Context({ update_id: 1, callback_query: { id: 'DEMO-CALLBACK', from: { id: sender, is_bot: false, first_name: 'DEMO' }, chat_instance: 'DEMO-CHAT', data, message: structuredClone(message) } }, bot.api, botInfo)
  }
  async function preview() {
    const result = await service.callTool('email_preview', { chat_id: '1', email: fixture })
    expect(result?.isError).not.toBeTrue()
    const message = messages.get(messages.size)!
    const id = message.reply_markup.inline_keyboard[0][0].callback_data.split(':')[1] as string
    return { message, id }
  }
  async function tap(message: any, column: number, sender = 1) {
    return service.handleCallback(context(message.reply_markup.inline_keyboard[0][column].callback_data, message, sender))
  }
  async function confirm() {
    const { message, id } = await preview()
    await tap(message, 0); await tap(message, 1)
    expect(store.get(id)?.phase).toBe('authorized')
    return { message, id }
  }
  return { service, options, bot, calls, messages, injections, sends, events, failures, context, preview, tap, confirm,
    intercept: (callback?: (method: string, payload: any) => Promise<void>) => { onCall = callback } }
}

describe('canonical email contract', () => {
  test('canonical digest ignores object insertion order and omitted empty recipient lists only', () => {
    const first = { to: ['demo@example.invalid'], subject: 'DEMO café ✉', body: 'Line one\nLine two' }
    const second = { body: first.body, bcc: [], cc: [], subject: first.subject, to: first.to }
    expect(canonicalEmailJson(first)).toBe('{"bcc":[],"body":"Line one\\nLine two","cc":[],"subject":"DEMO café ✉","to":["demo@example.invalid"]}')
    expect(emailDigest(first)).toBe(emailDigest(second))
    expect(emailDigest(first)).toBe('d5e25cc947ef111213510e5b11d285e3109c062b94923f04500d7dd9a8ae9573')
    expect(emailDigest({ ...first, to: ['Demo@example.invalid'] })).not.toBe(emailDigest(first))
  })

  test('preview includes every exact recipient, subject and body without truncation', () => {
    expect(emailText(fixture)).toBe('To: demo@example.invalid, second@example.invalid\nCc: copy@example.invalid\nBcc: blind@example.invalid\nSubject: DEMO ONLY\n\nOffline consent fixture. Never send.')
    expect(() => emailText({ ...fixture, body: 'x'.repeat(3900) })).toThrow()
  })

  test('hidden headers, unknown fields, malformed addresses and lone surrogates are refused', () => {
    for (const bad of [
      { ...fixture, subject: 'DEMO\r\nBcc: another@example.invalid' }, { ...fixture, from: 'extra@example.invalid' },
      { ...fixture, to: [] }, { ...fixture, cc: ['copy@example.invalid\n'] }, { ...fixture, bcc: 'other@example.invalid' },
      { ...fixture, body: '\u0000' }, { ...fixture, subject: '\ud800' }, { ...fixture, body: '\udc00' },
    ]) expect(() => canonicalEmailJson(bad)).toThrow()
  })

  test('email tool descriptions stay within the 2000-character cap', () => {
    for (const tool of emailTools) expect(tool.description!.length).toBeLessThanOrEqual(2000)
  })
})

describe('durable one-use consent', () => {
  test('two taps are required and the 600-second record is mode 600', () => {
    const record = store.preview('1', 42, fixture)
    expect(record.id).toMatch(/^[a-f0-9]{24}$/)
    expect(record.expires).toBe(now + 600_000)
    expect(statSync(store.path).mode & 0o777).toBe(0o600)
    expect(() => store.consume(record.id, '1', fixture)).toThrow()
    store.tap(record.id, 'prepare', '1', 42, emailText(fixture))
    expect(() => store.consume(record.id, '1', fixture)).toThrow()
    store.tap(record.id, 'confirm', '1', 42, emailText(fixture))
    expect(store.consume(record.id, '1', fixture).phase).toBe('consumed')
    expect(() => store.consume(record.id, '1', fixture)).toThrow()
  })

  test('all content mutations and cross-chat consumption require new consent', () => {
    const record = authorized()
    for (const email of [
      { ...fixture, to: ['other@example.invalid'] }, { ...fixture, to: [...fixture.to].reverse() },
      { ...fixture, cc: [] }, { ...fixture, bcc: [] }, { ...fixture, subject: 'DEMO changed' },
      { ...fixture, body: fixture.body + ' ' },
    ]) expect(() => store.consume(record.id, '1', email)).toThrow()
    expect(() => store.consume(record.id, '2', fixture)).toThrow()
    expect(store.get(record.id)?.phase).toBe('authorized')
  })

  test('copied message, changed visible text and exact expiry boundary reject', () => {
    const record = store.preview('1', 42, fixture)
    expect(() => store.tap(record.id, 'prepare', '1', 43, emailText(fixture))).toThrow()
    expect(() => store.tap(record.id, 'prepare', '1', 42, 'Changed')).toThrow()
    now = record.expires
    expect(() => store.tap(record.id, 'prepare', '1', 42, emailText(fixture))).toThrow()
  })

  test('expiry after authorization prevents sending', () => {
    const record = authorized()
    now = record.expires
    expect(() => store.consume(record.id, '1', fixture)).toThrow()
  })

  test('cancellation is audited but a restart never imports consent', () => {
    const record = authorized()
    store.invalidatePreview('1', 42)
    const restarted = new ConsentStore(state, () => now)
    expect(restarted.get(record.id)).toBeUndefined()
    expect(JSON.parse(readFileSync(store.path, 'utf8'))[record.id].phase).toBe('cancelled')
    expect(() => restarted.consume(record.id, '1', fixture)).toThrow()
  })

  test('a consumed record cannot replay through another store instance', () => {
    const record = authorized()
    const second = new ConsentStore(state, () => now)
    store.consume(record.id, '1', fixture)
    expect(() => second.consume(record.id, '1', fixture)).toThrow()
    store.invalidatePreview('1', 42)
    expect(second.get(record.id)).toBeUndefined()
    expect(JSON.parse(readFileSync(store.path, 'utf8'))[record.id].phase).toBe('consumed')
  })

  test('malformed storage, permissive modes and an existing transaction lock fail closed', () => {
    const record = authorized()
    chmodSync(store.path, 0o644)
    expect(() => store.consume(record.id, '1', fixture)).toThrow()
    chmodSync(store.path, 0o600)
    writeFileSync(join(state, 'consents.lock'), 'DEMO LOCK', { mode: 0o600 })
    expect(() => store.consume(record.id, '1', fixture)).toThrow()
    rmSync(join(state, 'consents.lock'))
    writeFileSync(store.path, 'malformed')
    expect(() => store.consume(record.id, '1', fixture)).toThrow()
  })
})

describe('email service confirmation', () => {
  test('poller spends one nonce for only the confirmed preview digest', async () => {
    let h!: ReturnType<typeof harness>
    h = harness({ runBackend: async (_file, _op, payload) => {
      const address = (h.service as any).authority.address()
      const query = (value: object): Promise<{ ok: boolean }> => new Promise((resolve, reject) => {
        const socket = connect(address.port, '127.0.0.1')
        let reply = ''
        socket.on('connect', () => socket.write(JSON.stringify(value) + '\n'))
        socket.on('data', chunk => {
          reply += chunk.toString('utf8')
          if (reply.includes('\n')) { socket.end(); resolve(JSON.parse(reply)) }
        })
        socket.on('error', reject)
      })
      const proof = { nonce: payload.nonce, consent_id: payload.consent_id,
        chat: payload.chat, message: payload.message, digest: payload.digest }
      expect(await query({ ...proof, digest: '0'.repeat(64) })).toEqual({ ok: false })
      expect(await query(proof)).toEqual({ ok: true })
      expect(await query(proof)).toEqual({ ok: false })
      return { ok: true }
    } })
    const { id } = await h.confirm()
    expect((await h.service.callTool('email_send', { chat_id: '1', consent_id: id, email: fixture }))?.isError).not.toBeTrue()
  })

  test('editing the audit receipt cannot manufacture a Confirm send tap', async () => {
    const h = harness(), { id } = await h.preview()
    const audit = JSON.parse(readFileSync(store.path, 'utf8'))
    audit[id].phase = 'authorized'
    writeFileSync(store.path, JSON.stringify(audit))
    const result = await h.service.callTool('email_send', { chat_id: '1', consent_id: id, email: fixture })
    expect(result?.isError).toBeTrue()
    expect(h.sends).toHaveLength(0)
  })

  test('controller startup cancels every unfinished phase and preserves completed history', () => {
    const preview = store.preview('1', 40, fixture)
    const confirm = store.preview('1', 41, fixture)
    store.tap(confirm.id, 'prepare', '1', 41, emailText(fixture))
    const approved = authorized()
    const consumed = authorized()
    const consumedBefore = store.consume(consumed.id, '1', fixture)
    const cancelled = store.preview('1', 43, fixture)
    const cancelledBefore = store.tap(cancelled.id, 'cancel', '1', 43, emailText(fixture))
    harness()
    const audit = JSON.parse(readFileSync(store.path, 'utf8'))
    for (const record of [preview, confirm, approved]) {
      expect(store.get(record.id)).toBeUndefined()
      expect(audit[record.id].phase).toBe('cancelled')
    }
    expect(audit[consumed.id]).toEqual(consumedBefore)
    expect(audit[cancelled.id]).toEqual(cancelledBefore)
    expect(statSync(store.path).mode & 0o777).toBe(0o600)
  })

  test('restart requires a fresh preview even if the old authorized email is still within its deadline', async () => {
    const h = harness(), { id } = await h.confirm()
    expect(store.get(id)!.expires).toBeGreaterThan(now)
    const restarted = new EmailService(h.options)
    expect((await restarted.callTool('email_send', { chat_id: '1', consent_id: id, email: fixture }))?.isError).toBeTrue()
    expect(restarted.consents.get(id)).toBeUndefined()
    expect(JSON.parse(readFileSync(store.path, 'utf8'))[id].phase).toBe('cancelled')
    expect(h.sends).toHaveLength(0)
    expect((await restarted.callTool('email_preview', { chat_id: '1', email: fixture }))?.isError).not.toBeTrue()
    const message = h.messages.get(h.messages.size)!
    const freshId = message.reply_markup.inline_keyboard[0][0].callback_data.split(':')[1]
    await restarted.handleCallback(h.context(`mail:${freshId}:prepare`, message))
    await restarted.handleCallback(h.context(`mail:${freshId}:confirm`, message))
    expect((await restarted.callTool('email_send', { chat_id: '1', consent_id: freshId, email: fixture }))?.isError).not.toBeTrue()
    expect(h.sends).toHaveLength(1)
  })

  test('old confirmation keyboards cannot restore consent after a controller restart', async () => {
    const h = harness(), { message, id } = await h.preview()
    await h.tap(message, 0)
    const oldKeyboard = structuredClone(message)
    const restarted = new EmailService(h.options)
    await restarted.handleCallback(h.context(`mail:${id}:confirm`, oldKeyboard))
    expect(restarted.consents.get(id)).toBeUndefined()
    expect(h.injections).toHaveLength(0)
  })

  test('controller startup fails closed when pending-consent invalidation cannot be persisted', () => {
    const h = harness()
    const record = authorized()
    writeFileSync(join(state, 'consents.lock'), 'DEMO active transaction', { mode: 0o600 })
    expect(() => new EmailService(h.options)).toThrow()
    expect(store.get(record.id)?.phase).toBe('authorized')
    rmSync(join(state, 'consents.lock'))
    chmodSync(store.path, 0o644)
    expect(() => new EmailService(h.options)).toThrow()
    chmodSync(store.path, 0o600)
    writeFileSync(store.path, 'malformed')
    expect(() => new EmailService(h.options)).toThrow()
    expect(h.sends).toHaveLength(0)
  })

  test('full two-tap flow swaps Cancel left and Confirm right, then sends once', async () => {
    const h = harness(), { message, id } = await h.preview()
    expect(message.text).toBe(emailText(fixture))
    expect(message.reply_markup.inline_keyboard[0].map((button: any) => button.text)).toEqual(['✉️ Send it', '✏️ Edit', '⏸ Hold'])
    expect((await h.service.callTool('email_send', { chat_id: '1', consent_id: id, email: fixture }))?.isError).toBeTrue()
    await h.tap(message, 0)
    expect(message.reply_markup.inline_keyboard[0].map((button: any) => button.text)).toEqual(['✖ Cancel', '✅ Confirm send'])
    expect(h.injections).toHaveLength(0)
    expect((await h.service.callTool('email_send', { chat_id: '1', consent_id: id, email: fixture }))?.isError).toBeTrue()
    const snapshot = structuredClone(message)
    await h.tap(message, 1)
    await h.tap(snapshot, 1)
    expect(h.injections.map(value => value.id)).toEqual([id])
    expect(message.reply_markup.inline_keyboard).toEqual([])
    const result = await h.service.callTool('email_send', { chat_id: '1', consent_id: id, email: fixture })
    expect(result?.isError).not.toBeTrue()
    expect(h.sends).toHaveLength(1)
    expect(h.sends[0]!.executable).toBe(join(fleet, 'bin', 'fleet-email'))
    expect(h.sends[0]!.payload.email).toEqual(JSON.parse(canonicalEmailJson(fixture)))
    expect((await h.service.callTool('email_send', { chat_id: '1', consent_id: id, email: fixture }))?.isError).toBeTrue()
    expect(h.sends).toHaveLength(1)
    expect(h.events.map(value => value.event)).toEqual(['EMAIL-PREVIEW', 'SENDIT-SWAP', 'EMAIL-CONFIRM', 'EMAIL-SEND', 'EMAIL-SEND'])
    expect(JSON.stringify(h.events)).not.toContain(fixture.body)
  })

  test('an inbox reply links a bounded full read to the confirmed send', async () => {
    const h = harness(), read = 'b'.repeat(24)
    expect((await h.service.callTool('email_preview', { chat_id: '1', email: fixture, read_receipt: read }))?.isError).toBeTrue()
    mkdirSync(join(state, 'mail-reads'))
    writeFileSync(join(state, 'mail-reads', `${read}.json`), JSON.stringify({ id: '7', at: Date.now() / 1000, full: true }), { mode: 0o600 })
    expect((await h.service.callTool('email_preview', { chat_id: '1', email: fixture, read_receipt: read }))?.isError).not.toBeTrue()
    const message = h.messages.get(h.messages.size)!
    const id = message.reply_markup.inline_keyboard[0][0].callback_data.split(':')[1] as string
    await h.tap(message, 0); await h.tap(message, 1)
    expect((await h.service.callTool('email_send', { chat_id: '1', consent_id: id, email: fixture }))?.isError).not.toBeTrue()
    expect(h.events.some(event => event.event === 'EMAIL-REPLY' && event.fields.id === id && event.fields.read === read && event.fields.status === 'sent')).toBe(true)
  })

  test('Cancel consumes the keyboard without an injected message or backend call', async () => {
    const h = harness(), { message, id } = await h.preview()
    await h.tap(message, 0)
    const snapshot = structuredClone(message)
    await h.tap(message, 0); await h.tap(snapshot, 1)
    expect(store.get(id)?.phase).toBe('cancelled')
    expect(h.injections).toHaveLength(0)
    expect(h.sends).toHaveLength(0)
  })

  test('a confirm callback cannot skip the first tap or reuse its old keyboard', async () => {
    const h = harness(), { message, id } = await h.preview()
    await h.service.handleCallback(h.context(`mail:${id}:confirm`, message))
    expect(store.get(id)?.phase).toBe('preview')
    const old = structuredClone(message)
    await h.tap(message, 0)
    await h.service.handleCallback(h.context(`mail:${id}:confirm`, old))
    expect(store.get(id)?.phase).toBe('confirm')
    expect(h.injections).toHaveLength(0)
  })

  test('simultaneous prepare, confirm and send attempts each execute once', async () => {
    const h = harness(), { message, id } = await h.preview()
    const first = structuredClone(message)
    await Promise.all([h.tap(first, 0), h.tap(first, 0)])
    const second = structuredClone(message)
    await Promise.all([h.tap(second, 1), h.tap(second, 1)])
    expect(h.injections).toHaveLength(1)
    const sends = await Promise.all([1, 2].map(() => h.service.callTool('email_send', { chat_id: '1', consent_id: id, email: fixture })))
    expect(sends.filter(result => !result?.isError)).toHaveLength(1)
    expect(h.sends).toHaveLength(1)
  })

  test('an uncertain backend result spends consent before invocation and never retries', async () => {
    let attempts = 0
    const h = harness({ runBackend: async (_file, _op, payload) => {
      attempts++
      expect(store.get(payload.consent_id)?.phase).toBe('consumed')
      throw Error('DEMO timeout with sensitive output')
    } })
    const { id } = await h.confirm()
    const first = await h.service.callTool('email_send', { chat_id: '1', consent_id: id, email: fixture })
    const second = await h.service.callTool('email_send', { chat_id: '1', consent_id: id, email: fixture })
    expect(first?.isError).toBeTrue(); expect(second?.isError).toBeTrue()
    expect(attempts).toBe(1)
    expect(JSON.stringify(first)).toContain('Do not retry automatically')
    expect(JSON.stringify(first)).not.toContain('sensitive output')
  })

  test('a backend response without explicit success remains uncertain', async () => {
    const h = harness({ runBackend: async () => ({ accepted: 'DEMO only' }) })
    const { id } = await h.confirm()
    expect((await h.service.callTool('email_send', { chat_id: '1', consent_id: id, email: fixture }))?.isError).toBeTrue()
    expect(store.get(id)?.phase).toBe('consumed')
  })

  test('oversized and invalid previews produce no Telegram calls', async () => {
    const h = harness()
    for (const email of [{ ...fixture, body: 'x'.repeat(3900) }, { ...fixture, to: [] }]) {
      expect((await h.service.callTool('email_preview', { chat_id: '1', email }))?.isError).toBeTrue()
    }
    expect(h.calls).toHaveLength(0)
  })

  test('a partially failed preview leaves no active consent', async () => {
    const h = harness()
    h.failures.add('editMessageReplyMarkup')
    expect((await h.service.callTool('email_preview', { chat_id: '1', email: fixture }))?.isError).toBeTrue()
    const records = Object.values(JSON.parse(readFileSync(store.path, 'utf8'))) as any[]
    expect(records).toHaveLength(1)
    expect(records[0].phase).toBe('cancelled')
    expect(h.injections).toHaveLength(0)
  })

  test('failed sendMessage produces no consent record and redacts Telegram error details', async () => {
    const h = harness()
    h.failures.add('sendMessage')
    const result = await h.service.callTool('email_preview', { chat_id: '1', email: fixture })
    expect(result?.isError).toBeTrue()
    expect(JSON.stringify(result)).not.toContain('private backend detail')
    expect(existsSync(store.path)).toBeFalse()
  })

  test('callback API failures are caught and never inject approval', async () => {
    const h = harness(), { message, id } = await h.preview()
    h.failures.add('editMessageReplyMarkup'); h.failures.add('answerCallbackQuery')
    await expect(h.tap(message, 0)).resolves.toBeTrue()
    expect(store.get(id)?.phase).toBe('cancelled')
    expect(h.injections).toHaveLength(0)
  })

  test('confirmation and injection failures cancel authorization', async () => {
    const h = harness({ injectConfirm: async () => { throw Error('DEMO uncertain injection') } })
    const { message, id } = await h.preview()
    await h.tap(message, 0); await h.tap(message, 1)
    expect(store.get(id)?.phase).toBe('cancelled')
    expect((await h.service.callTool('email_send', { chat_id: '1', consent_id: id, email: fixture }))?.isError).toBeTrue()
  })

  test('a preview edit during the prepare request cancels the callback', async () => {
    const h = harness(), { message, id } = await h.preview()
    h.intercept(async method => { if (method === 'editMessageReplyMarkup') h.service.beforeEdit('1', message.message_id) })
    await h.tap(message, 0)
    expect(store.get(id)?.phase).toBe('cancelled')
    expect(h.injections).toHaveLength(0)
  })
})

describe('email security gates', () => {
  test('group, inline, forwarded, copied and foreign-bot messages cannot authorize', async () => {
    const h = harness(), { message, id } = await h.preview()
    for (const changed of [
      { ...message, chat: { id: -1, type: 'group' } }, { ...message, message_id: message.message_id + 10 },
      { ...message, chat: { id: 2, type: 'private' } }, { ...message, text: 'Changed preview' },
      { ...message, from: { ...botInfo, id: 999 } }, { ...message, forward_origin: { type: 'hidden_user', date: 1, sender_user_name: 'DEMO' } },
      { ...message, via_bot: botInfo }, { ...message, reply_markup: { inline_keyboard: [] } },
    ]) await h.service.handleCallback(h.context(`mail:${id}:prepare`, changed))
    const inline = h.context(`mail:${id}:prepare`, message)
    delete inline.update.callback_query!.message
    inline.update.callback_query!.inline_message_id = 'DEMO-INLINE'
    await h.service.handleCallback(inline)
    expect(store.get(id)?.phase).toBe('preview')
    expect(h.injections).toHaveLength(0)
  })

  test('foreign sender, malformed callbacks and reactions do not authorize email', async () => {
    const h = harness(), { message, id } = await h.preview()
    await h.tap(message, 0, 2)
    for (const data of [`mail:${id}:prepare:extra`, `mail:../invalid:prepare`, `mail:${id}:reaction`]) {
      expect(await h.service.handleCallback(h.context(data, message))).toBeTrue()
    }
    expect(await h.service.handleCallback(h.context('qr:0:0', message))).toBeFalse()
    expect(await h.service.handleCallback(new Context({ update_id: 2, message_reaction: {
      chat: { id: 1, type: 'private', first_name: 'DEMO' }, message_id: message.message_id, date: 1,
      user: { id: 1, is_bot: false, first_name: 'DEMO' }, old_reaction: [], new_reaction: [{ type: 'emoji', emoji: '👍' }],
    } }, h.bot.api, botInfo))).toBeFalse()
    expect(store.get(id)?.phase).toBe('preview')
    expect(h.injections).toHaveLength(0)
  })

  test('every press rereads revoked or malformed on-disk allowlists', async () => {
    const h = harness(), { message, id } = await h.preview()
    for (const access of [{ dmPolicy: 'disabled', allowFrom: ['1'] }, { allowFrom: [] }, { allowFrom: '12' }, { allowFrom: [1] }]) {
      writeFileSync(join(state, 'access.json'), JSON.stringify(access))
      await h.tap(message, 0)
      expect(store.get(id)?.phase).toBe('preview')
    }
    expect(h.injections).toHaveLength(0)
  })

  test('a revoked allowlist between taps and before send always refuses', async () => {
    const h = harness(), { message, id } = await h.preview()
    await h.tap(message, 0)
    writeFileSync(join(state, 'access.json'), JSON.stringify({ allowFrom: [] }))
    await h.tap(message, 1)
    expect(h.injections).toHaveLength(0)
    writeFileSync(join(state, 'access.json'), JSON.stringify({ allowFrom: ['1'] }))
    await h.tap(message, 1)
    expect(h.injections).toHaveLength(1)
    writeFileSync(join(state, 'access.json'), JSON.stringify({ allowFrom: [] }))
    expect((await h.service.callTool('email_send', { chat_id: '1', consent_id: id, email: fixture }))?.isError).toBeTrue()
    expect(h.sends).toHaveLength(0)
  })

  test('a revoked allowlist while the confirmation keyboard is being removed cancels consent', async () => {
    const h = harness(), { message, id } = await h.preview()
    await h.tap(message, 0)
    h.intercept(async method => { if (method === 'editMessageReplyMarkup') writeFileSync(join(state, 'access.json'), '{"allowFrom":[]}') })
    await h.tap(message, 1)
    expect(store.get(id)?.phase).toBe('cancelled')
    expect(h.injections).toHaveLength(0)
  })

  test('send rejects cross-chat, changed content, expiry and edited previews', async () => {
    const h = harness(), { message, id } = await h.confirm()
    for (const args of [
      { chat_id: '2', consent_id: id, email: fixture }, { chat_id: '1', consent_id: id, email: { ...fixture, bcc: [] } },
    ]) expect((await h.service.callTool('email_send', args))?.isError).toBeTrue()
    h.service.beforeEdit('1', String(message.message_id))
    expect((await h.service.callTool('email_send', { chat_id: '1', consent_id: id, email: fixture }))?.isError).toBeTrue()
    const next = await h.confirm()
    now += 600_000
    expect((await h.service.callTool('email_send', { chat_id: '1', consent_id: next.id, email: fixture }))?.isError).toBeTrue()
    expect(h.sends).toHaveLength(0)
  })

  test('disabled email, relative backend paths and unlisted tool chats are refused', async () => {
    const h = harness()
    for (const config of [{ enabled: false, backend: 'builtin' }, { enabled: true, backend: './bad' }, { enabled: true, backend: '/bad\npath' }]) {
      writeFileSync(join(fleet, 'email.json'), JSON.stringify(config))
      expect(h.service.isConfigured()).toBeFalse()
      expect((await h.service.callTool('email_preview', { chat_id: '1', email: fixture }))?.isError).toBeTrue()
    }
    writeFileSync(join(fleet, 'email.json'), JSON.stringify({ enabled: true, backend: 'builtin' }))
    for (const chat_id of ['-1', '3', '01', '1\n']) {
      expect((await h.service.callTool('email_preview', { chat_id, email: fixture }))?.isError).toBeTrue()
    }
    expect(h.calls).toHaveLength(0)
    expect(await h.service.callTool('unknown', {})).toBeNull()
  })

  test('the configured custom executable receives send argv and JSON stdin without a shell', async () => {
    const report = join(root, 'backend-report.json'), backend = join(root, 'custom backend')
    writeFileSync(backend, `#!${process.execPath}\nconst fs = require('node:fs');\nconst path = require('node:path');\n(async () => {\nconst payload = JSON.parse(await Bun.stdin.text());\nconst record = JSON.parse(fs.readFileSync(path.join(process.env.TELEGRAM_STATE_DIR, 'consents.json'), 'utf8'))[payload.consent_id];\nconst dir = path.join(process.env.TELEGRAM_STATE_DIR, 'mail-attempts');\nfs.mkdirSync(dir, {recursive:true, mode:448});\nconst fd = fs.openSync(path.join(dir, payload.consent_id), 'wx', 384); fs.closeSync(fd);\nfs.writeFileSync(${JSON.stringify(report)}, JSON.stringify({argv:process.argv.slice(2),payload,phase:record.phase,tokenPresent:Boolean(process.env.TELEGRAM_BOT_TOKEN)}), {mode:384});\nprocess.stdout.write(JSON.stringify({ok:true}));\n})();\n`, { mode: 0o700 })
    mkdirSync(join(fleet, 'bin'))
    writeFileSync(join(fleet, 'bin', 'fleet-email'), readFileSync(join(import.meta.dir, '../../engine/fleet-email')), { mode: 0o700 })
    writeFileSync(join(fleet, 'fleet.json'), JSON.stringify({ version: 2, bots: [{ key: 'main', state_dir: state }] }))
    writeFileSync(join(fleet, 'email.json'), JSON.stringify({ enabled: true, backend }))
    const h = harness({ runBackend: undefined }), { id } = await h.confirm()
    expect((await h.service.callTool('email_send', { chat_id: '1', consent_id: id, email: fixture }))?.isError).not.toBeTrue()
    const receipt = JSON.parse(readFileSync(report, 'utf8'))
    expect(receipt.argv).toEqual(['send'])
    expect(receipt.phase).toBe('consumed')
    expect(receipt.payload).toEqual({ consent_id: id, bot: 'main', email: JSON.parse(canonicalEmailJson(fixture)) })
    expect(receipt.tokenPresent).toBeFalse()
    expect(existsSync(join(state, 'mail-attempts', id))).toBeTrue()
    expect(statSync(join(state, 'mail-attempts', id)).mode & 0o777).toBe(0o600)
  })
})
