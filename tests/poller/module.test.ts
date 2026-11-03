import { afterEach, describe, expect, setDefaultTimeout, test } from 'bun:test'
import { Bot, Context, GrammyError, InlineKeyboard } from 'grammy'
import type { Message, Update, UserFromGetMe } from 'grammy/types'
import { existsSync, mkdirSync, mkdtempSync, readFileSync, realpathSync, rmSync, writeFileSync } from 'node:fs'
import { basename, join, resolve, sep } from 'node:path'
import { execFileSync } from 'node:child_process'
import { DEFAULT_UX, IN_SESSION_ENGINES, TelebotsModule, addedReactionEmoji, engineSpawnOptions, formatVoiceDuration, init, keyText, replyButtons, shq, validatedApiRoot, voiceBody } from '../../poller/telebots.ts'
import type { AttachmentMeta, ExecOptions, TelebotsDependencies } from '../../poller/contracts.ts'
import ux from '../../poller/ux.json'

// The runner must never signal an owner process after a wall-clock timeout.
// Engine and voice timeout cases use injected results, without child processes.
setDefaultTimeout(0)

const base = process.env.TELEBOTS_SANDBOX_ROOT
if (!base || process.env.TELEBOTS_TEST !== '1' || process.env.TELEGRAM_BOT_TOKEN) throw new Error('Use the scrubbed sandbox wrapper for module tests')
for (const name of ['HOME', 'TMPDIR', 'FLEET_HOME', 'TELEGRAM_STATE_DIR']) {
  const path = process.env[name]
  if (!path || !resolve(path).startsWith(resolve(base) + sep)) throw new Error(`Sandbox escape: ${name}`)
}
const safeBase = realpathSync(base)
const info: UserFromGetMe = { id: 900, is_bot: true, first_name: 'Test', username: 'test_bot', can_join_groups: true, can_read_all_group_messages: false, supports_inline_queries: false, can_connect_to_business: false, has_main_web_app: false, has_topics_enabled: false, allows_users_to_create_topics: false }
type Call = { method: string; payload: Record<string, unknown> }
type Notice = { method: string; params: { content: string; meta: Record<string, string> } }
const fixtures: Array<{ root: string; tb: TelebotsModule }> = []
let updateId = 0

function fixture(overrides: Partial<TelebotsDependencies> = {}) {
  const root = mkdtempSync(join(safeBase, 'module-'))
  const state = join(root, 'state'), fleet = join(root, 'fleet'), workdir = join(root, 'work dir')
  for (const path of [state, fleet, join(fleet, 'bin'), workdir]) mkdirSync(path, { recursive: true })
  const fleetBots = [{ key: 'main', name: 'Main', state_dir: state, workdir }, { key: 'second', name: 'Second', state_dir: join(root, 'second-state'), workdir: join(root, 'second-work') }]
  writeFileSync(join(state, 'access.json'), JSON.stringify({ dmPolicy: 'allowlist', allowFrom: ['101', '202'] }))
  writeFileSync(join(fleet, 'fleet.json'), JSON.stringify({ version: 2, bots: fleetBots }))
  for (const name of ['fresh', 'model', 'esc', 'compact', 'update', 'context', 'usage', 'resume-open']) writeFileSync(join(fleet, 'bin', `fleet-${name}`), '#!/bin/sh\nexit 0\n', { mode: 0o700 })
  const calls: Call[] = [], notices: Notice[] = [], spawns: Array<{ file: string; args: string[]; log: string }> = [], runs: Array<{ file: string; args: string[]; options: ExecOptions }> = [], scheduled: Array<{ fn: () => void; ms: number }> = []
  const messages = new Map<string, Message.TextMessage>()
  let messageCounter = 300
  let fail: (method: string, payload: Record<string, unknown>) => boolean = () => false
  let beforeApi: (method: string, payload: Record<string, unknown>) => Promise<void> = async () => {}
  let output: (file: string, args: string[]) => Promise<{ stdout: string }> = async file => ({ stdout: basename(file) === 'fleet-state' ? '[]' : 'known words' })
  const bot = new Bot('offline-test-token', { botInfo: info })
  bot.api.config.use(async (_prev, method, payload) => {
    const p = structuredClone(payload) as Record<string, unknown>
    calls.push({ method, payload: p })
    await beforeApi(method, p)
    if (fail(method, p)) throw new Error('Offline API refusal')
    if (method === 'sendMessage') {
      const message = { message_id: ++messageCounter, date: 1000, chat: { id: Number(p.chat_id), type: 'private', first_name: 'Owner' }, from: info, text: p.text, ...(p.reply_markup ? { reply_markup: p.reply_markup } : {}) } as Message.TextMessage
      messages.set(`${p.chat_id}:${message.message_id}`, message)
      return { ok: true, result: message } as never
    }
    if (method === 'editMessageText') {
      const key = `${p.chat_id}:${p.message_id}`
      const previous = messages.get(key)
      const message = { ...previous, text: p.text, reply_markup: p.reply_markup } as Message.TextMessage
      messages.set(key, message)
      return { ok: true, result: message } as never
    }
    if (method === 'editMessageReplyMarkup') {
      const key = `${p.chat_id}:${p.message_id}`
      const previous = messages.get(key)
      if (previous) messages.set(key, { ...previous, reply_markup: p.reply_markup } as Message.TextMessage)
    }
    return { ok: true, result: true } as never
  })
  const tb = init({ bot, stateDir: state, token: 'offline-test-token', fleetHome: fleet, ownerPid: 12345,
    mcp: { notification: async message => { notices.push(message) } }, loadAccess: () => ({ ackReaction: '👍' }), assertAllowedChat: chat => { if (!['101', '202'].includes(chat)) throw new Error('Not allowed') },
    runOutput: async (file, args, options) => { runs.push({ file, args, options }); return output(file, args) },
    spawnDetached: (file, args, log) => { spawns.push({ file, args, log }) },
    downloadFile: async () => ({ path: join(state, 'inbox', 'voice.ogg'), size: 10 }),
    schedule: (fn, ms) => { scheduled.push({ fn, ms }); return { unref() {} } },
    ...overrides,
  }) as TelebotsModule
  tb.registerHandlers()
  fixtures.push({ root, tb })
  const textContext = (text = 'hi', chatId = 101) => new Context({ update_id: ++updateId, message: { message_id: updateId, date: 1000, chat: { id: chatId, type: 'private', first_name: 'Owner' }, from: { id: chatId, is_bot: false, first_name: 'Owner' }, text } }, bot.api, info)
  const message = (data: string, label = 'Choice', id = ++messageCounter): Message.TextMessage => ({ message_id: id, date: 1000, chat: { id: 101, type: 'private', first_name: 'Owner' }, from: info, text: 'Question', reply_markup: new InlineKeyboard().text(label, data) })
  const callbackUpdate = (data: string, msg: Message.TextMessage = message(data), userId = 101): Update => ({ update_id: ++updateId, callback_query: { id: String(updateId), from: { id: userId, is_bot: false, first_name: 'Owner' }, chat_instance: 'offline', data, message: msg } })
  const tap = async (data: string, msg?: Message.TextMessage, userId?: number) => { await bot.handleUpdate(callbackUpdate(data, msg, userId)); await tb.drain() }
  return { root, state, fleet, workdir, fleetBots, calls, notices, spawns, runs, scheduled, bot, tb, messages, message, textContext, callbackUpdate, tap,
    setFail(fn: typeof fail) { fail = fn }, setOutput(fn: typeof output) { output = fn }, setBeforeApi(fn: typeof beforeApi) { beforeApi = fn },
    events: () => existsSync(join(state, 'logs', 'poller-events.log')) ? readFileSync(join(state, 'logs', 'poller-events.log'), 'utf8').trim().split('\n').map(line => line.slice(line.indexOf(' ') + 1)) : [],
    sent: () => calls.filter(call => call.method === 'sendMessage'),
    edited: () => calls.filter(call => call.method === 'editMessageText'),
    lastMessage: () => [...messages.values()].at(-1)!,
  }
}

afterEach(async () => { for (const { root, tb } of fixtures.splice(0)) { await tb.drain(); tb.dispose(); rmSync(root, { recursive: true, force: true }) } })

describe('UX and reply behavior', () => {
  test('compiled defaults equal the canonical 13-command table', () => {
    expect(DEFAULT_UX).toEqual(ux)
    expect(DEFAULT_UX.commands.map(c => c.command)).toEqual(['esc', 'fresh', 'update', 'key', 'inbox', 'opus', 'fable', 'model', 'effort', 'context', 'compact', 'usage', 'resume'])
    expect(keyText()).toBe(keyText(ux))
    for (const command of ux.commands) expect(keyText()).toContain(`/${command.command} - ${command.description}`)
    for (const reaction of ux.reactions) expect(keyText()).toContain(reaction.key_line)
    expect(keyText()).toContain('😁 explain it simply')
    expect(ux.reactions.find(reaction => reaction.emoji === '😁')?.phrase).toBe('Explain this simply')
  })
  test('3/2/1 layout uses the longest trimmed label and validates before sending', () => {
    expect(replyButtons([' a ', 'b', 'c', 'd'])!.inline_keyboard.map(row => row.length)).toEqual([3, 1])
    expect(replyButtons(['1234567890123', 'b', 'c'])!.inline_keyboard.map(row => row.length)).toEqual([2, 1])
    expect(replyButtons(['1234567890123456789', 'b', 'c'])!.inline_keyboard.map(row => row.length)).toEqual([1, 1, 1])
    expect(replyButtons(['a', 'b', 'c', 'd'])!.inline_keyboard[1][0]).toEqual({ text: 'd', callback_data: 'qr:1:0' })
    for (const raw of [5, 'yes', [''], [null], [1], ['x'.repeat(41)], Array(7).fill('yes'),
      ['/fresh\nExplain this command'], ['/model\rstart'], ['tab\tlabel'], ['hidden\u0000label'], ['delete\u007flabel']]) {
      expect(() => replyButtons(raw)).toThrow()
    }
    expect(replyButtons([])).toBeUndefined()
    expect(replyButtons(null)).toBeUndefined()
  })
  test('quick bar follows a successful plain first chunk, once per lifetime', () => {
    const { tb } = fixture(), buttons = replyButtons(['Yes'])
    expect(tb.replyMarkup(buttons, 0, 2)).toBeUndefined()
    expect(tb.replyMarkup(buttons, 1, 2)).toBe(buttons)
    const bar = tb.replyMarkup(undefined, 0, 2)
    expect(bar).toMatchObject({ keyboard: [[{ text: '/esc' }, { text: '/model' }], [{ text: '/usage' }, { text: '/fresh' }]], is_persistent: true })
    expect(tb.replyMarkup(undefined, 0, 2)).toBe(bar)
    tb.replySent(bar)
    expect(tb.replyMarkup(undefined, 0, 2)).toBeUndefined()
    expect(tb.replyMarkup(buttons, 0, 1)).toBe(buttons)
  })
  test('menu, key and inbox are exact, and caps require this process PID', async () => {
    const f = fixture()
    await f.tb.onPollStart(info)
    expect(f.calls.find(c => c.method === 'setMyCommands')?.payload).toEqual({ commands: ux.commands, scope: { type: 'all_private_chats' } })
    expect(f.tb.allowedUpdates).toEqual(['message', 'callback_query', 'message_reaction'])
    f.tb.afterPidWrite(); expect(existsSync(join(f.state, 'caps'))).toBe(false)
    writeFileSync(join(f.state, 'bot.pid'), String(process.pid)); f.tb.afterPidWrite()
    expect(readFileSync(join(f.state, 'caps'), 'utf8')).toBe(`resume-buttons=1\npid=${process.pid}\n`)
    expect(await f.tb.intercept(f.textContext(), '/KEY', '101')).toBe(true)
    expect(await f.tb.intercept(f.textContext(), '/inbox', '101')).toBe(true)
    await f.tb.drain()
    expect(f.sent().map(c => c.payload.text)).toEqual([keyText(), 'Inbox is not connected. On your Mac run: fleet-email setup'])
    expect(f.notices).toHaveLength(0)
  })
  test('test API override requires explicit loopback test mode and origin-only URL', () => {
    expect(validatedApiRoot('http://127.0.0.1:1234', true)).toBe('http://127.0.0.1:1234')
    expect(validatedApiRoot('http://[::1]:1234', true)).toBe('http://[::1]:1234')
    for (const url of ['https://example.com', 'http://localhost/path', 'http://a@localhost', 'file://localhost', 'http://127.0.0.1/?x=1', 'http://2130706433', 'http://0x7f000001', 'http://127.1']) expect(validatedApiRoot(url, true)).toBe('https://api.telegram.org')
    expect(validatedApiRoot('http://localhost:1234', false)).toBe('https://api.telegram.org')
  })
})

describe('callback safety and replies', () => {
  const families = ['eff:low', 'qr:0:0', 'md:opus', 'me:opus:low', 'ho:second', 'rs:aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee', 'rsx:refresh', 'mail:aaaaaaaaaaaaaaaaaaaaaaaa:prepare']
  test.each(families)('%s rejects groups and users revoked on disk', async data => {
    const f = fixture(), msg = f.message(data)
    await f.tap(data, msg, 303)
    expect(f.calls.at(-1)?.payload.text).toBe('Not authorized.')
    const group = { ...msg, chat: { id: -100, type: 'group', title: 'Group' } } as Message.TextMessage
    await f.tap(data, group)
    expect(f.calls.at(-1)?.payload.text).toBe('Only works in a direct message.')
    for (const access of [{ allowFrom: '01010' }, { allowFrom: ['101', 2] }, { dmPolicy: 'disabled', allowFrom: ['101'] }, null]) {
      writeFileSync(join(f.state, 'access.json'), JSON.stringify(access))
      await f.tap(data, msg)
      expect(f.calls.at(-1)?.payload.text).toBe('Not authorized.')
    }
    expect(f.spawns).toHaveLength(0); expect(f.notices).toHaveLength(0)
  })
  test('unknown and unanchored callback data continue to stock middleware', async () => {
    const f = fixture(); let stock = 0
    f.bot.on('callback_query:data', () => { stock++ })
    for (const data of ['perm:allow:abcde', 'eff:low;bad', 'md:opus\n', 'me:opus:bogus', 'qr:100:0', 'rs:../../file']) await f.tap(data)
    expect(stock).toBe(6); expect(f.spawns).toHaveLength(0)
  })
  test('QR reads the original cell, consumes once and never interprets slash labels', async () => {
    const f = fixture(), msg = f.message('qr:0:0', '/fresh')
    msg.entities = [{ type: 'bold', offset: 0, length: 8 }]
    await Promise.all([f.bot.handleUpdate(f.callbackUpdate('qr:0:0', msg)), f.bot.handleUpdate(f.callbackUpdate('qr:0:0', msg))])
    await f.tb.drain(); await f.tap('qr:0:0', msg)
    expect(f.notices).toHaveLength(1)
    expect(f.notices[0].params).toMatchObject({ content: '/fresh', meta: { via: 'button', tapped_message_id: String(msg.message_id), user_id: '101' } })
    expect(f.edited()[0].payload).toMatchObject({ text: 'Question\n▸ /fresh', entities: msg.entities })
    expect(f.spawns).toHaveLength(0)
    expect(existsSync(join(f.state, 'last-inbound'))).toBe(true)
  })
  test('stale and forged cells refuse; Telegram failures stay inside the handler', async () => {
    const f = fixture(), msg = f.message('qr:0:1', 'Other')
    await f.tap('qr:0:0', msg)
    expect(f.calls.at(-1)?.payload.text).toBe('Already answered.')
    f.setFail(() => true)
    await f.tap('qr:0:0', f.message('qr:0:0', 'Choice'))
    expect(f.notices[0].params.content).toBe('Choice')
  })
  test('Send it swaps Cancel left, Confirm right without authorizing email', async () => {
    const f = fixture(), msg = f.message('qr:0:0', '✉️ Send it')
    await f.tap('qr:0:0', msg)
    expect(f.notices).toHaveLength(0)
    const edit = f.edited()[0].payload
    expect(edit.reply_markup).toEqual({ inline_keyboard: [[{ text: '✖ Cancel', callback_data: 'qr:0:0' }, { text: '✅ Confirm send', callback_data: 'qr:0:1' }]] })
    await f.tap('qr:0:0', msg)
    expect(f.notices).toHaveLength(0)
    const swapped = { ...msg, text: String(edit.text), reply_markup: edit.reply_markup } as Message.TextMessage
    await f.tap('qr:0:1', swapped)
    expect(f.notices[0].params).toMatchObject({ content: '✅ Confirm send', meta: { via: 'button' } })
    expect(f.notices[0].params.meta.consent_id).toBeUndefined()
  })
  test('Compact now consumes its bubble and runs the compact engine', async () => {
    const f = fixture(); await f.tap('qr:0:0', f.message('qr:0:0', 'Compact now'))
    expect(f.spawns[0]).toMatchObject({ args: ['12345', f.workdir] })
    expect(basename(f.spawns[0].file)).toBe('fleet-compact'); expect(f.notices).toHaveLength(0)
  })
})

describe('commands and pickers', () => {
  test('only fleet-esc and fleet-compact keep the bot session; every other engine starts detached', () => {
    // The lone Esc goes in by TIOCSTI, which the kernel allows only on the caller's own terminal.
    for (const name of ['fleet-esc', 'fleet-compact']) {
      expect(IN_SESSION_ENGINES.test(name)).toBe(true)
      expect(engineSpawnOptions(join('/fleet', 'bin', name))).toEqual({ detached: false, stdio: 'ignore' })
    }
    const detached = ['fleet-fresh', 'fleet-model', 'fleet-update', 'fleet-context', 'fleet-usage', 'fleet-resume-open',
      'fleet-esc.sh', 'fleet-escape', 'fleet-compact-old', 'old-fleet-esc', 'FLEET-ESC', 'fleet-esc ']
    for (const name of detached) {
      expect(IN_SESSION_ENGINES.test(name)).toBe(false)
      expect(engineSpawnOptions(join('/fleet', 'bin', name))).toEqual({ detached: true, stdio: 'ignore' })
    }
    expect(engineSpawnOptions('/fleet/fleet-esc/bin/fleet-fresh').detached).toBe(true)
  })
  test('all intercepted commands use the C6 arguments and bounded compact focus', async () => {
    const f = fixture()
    const inputs = ['/fresh', '/Model opus low', '/opus', '/FABLE', '/effort HIGH', '/esc', '/update', '/context', '/usage', '/COMPACT keep\n  the\u0000 notes']
    for (const input of inputs) { expect(await f.tb.intercept(f.textContext(), input, '101')).toBe(true); await f.tb.drain() }
    expect(f.spawns.map(s => [basename(s.file), s.args])).toEqual([
      ['fleet-fresh', ['12345', f.workdir, 'fresh-reset']],
      ['fleet-model', [f.workdir, '101', 'opus low']], ['fleet-model', [f.workdir, '101', 'opus']], ['fleet-model', [f.workdir, '101', 'fable']], ['fleet-model', [f.workdir, '101', 'high']],
      ['fleet-esc', ['12345', f.workdir, '101']],
      ['fleet-update', ['12345', f.workdir, '101']], ['fleet-context', [f.workdir, '101']], ['fleet-usage', [f.workdir, '101']], ['fleet-compact', ['12345', f.workdir, 'keep the notes']],
    ])
    for (const spawn of f.spawns) expect(spawn.log.startsWith(join(f.state, 'logs') + sep)).toBe(true)
    await f.tb.intercept(f.textContext(), '/compact ' + 'x'.repeat(500), '101'); await f.tb.drain()
    expect(f.spawns.at(-1)?.args[2].length).toBe(300)
    expect(shq("a'b;$(none)")).toBe("'a'\\''b;$(none)'")
  })
  test('noncommands, group commands, /resume and configured inbox remain forwarded', async () => {
    const f = fixture()
    for (const input of ['/Fresh', '/fresh now', '/update now', '/key now', '/compacted', 'please /esc', '/resume', '/efforts']) expect(await f.tb.intercept(f.textContext(), input, '101')).toBe(false)
    const group = new Context({ update_id: ++updateId, message: { message_id: updateId, date: 1, chat: { type: 'group', id: -1, title: 'Group' }, from: { id: 101, is_bot: false, first_name: 'Owner' }, text: '/model' } }, f.bot.api, info)
    expect(await f.tb.intercept(group, '/model', '-1')).toBe(false)
    writeFileSync(join(f.fleet, 'email.json'), JSON.stringify({ enabled: true, backend: 'builtin' }))
    expect(await f.tb.intercept(f.textContext(), '/inbox', '101')).toBe(false)
  })
  test('plain model aliases mark dots and model then effort produces one restart', async () => {
    const f = fixture(); writeFileSync(join(f.state, 'model-state'), 'MODEL=opus\nEFFORT=high\n')
    await f.tb.intercept(f.textContext(), '/model', '101'); await f.tb.drain()
    let msg = f.lastMessage()
    expect(msg.reply_markup?.inline_keyboard.map(row => row.length)).toEqual([4, 3, 2, 1])
    expect(msg.reply_markup?.inline_keyboard[0][0].text).toBe('● Opus')
    await f.tap('md:sonnet', msg)
    expect(f.spawns).toHaveLength(0)
    const edit = f.edited().at(-1)!.payload
    msg = { ...msg, text: String(edit.text), reply_markup: edit.reply_markup } as Message.TextMessage
    expect(msg.reply_markup?.inline_keyboard[0][2].text).toBe('● high')
    await f.tap('me:sonnet:low', msg)
    await f.tap('me:sonnet:low', msg)
    expect(f.spawns.map(s => s.args)).toEqual([[f.workdir, '101', 'sonnet low']])
  })
  test('effort invalid words show all levels and every close changes nothing', async () => {
    const f = fixture()
    await f.tb.intercept(f.textContext(), '/effort unknown', '101'); await f.tb.drain()
    const msg = f.lastMessage()
    expect(msg.text).toContain('Levels: low, medium, high, xhigh, max.')
    expect(msg.reply_markup?.inline_keyboard.flat()).toHaveLength(6)
    await f.tap('eff:close', msg)
    await f.tap('me:close')
    expect(f.spawns).toHaveLength(0)
    expect(f.edited().map(c => c.payload.text)).toEqual(['Effort picker closed.', 'Model picker closed.'])
  })
  test('missing targets and engine launch failures are honest, with no action', async () => {
    const f = fixture({ ownerPid: null })
    await f.tb.intercept(f.textContext(), '/esc', '101'); await f.tb.drain()
    expect(f.sent().at(-1)?.payload.text).toContain('Could not run /esc')
    rmSync(join(f.fleet, 'bin', 'fleet-model'))
    await f.tb.intercept(f.textContext(), '/model opus', '101'); await f.tb.drain()
    expect(f.sent().at(-1)?.payload.text).toContain('/model failed to launch')
    expect(f.spawns).toHaveLength(0)
  })
})

describe('handoff and resume', () => {
  test('editing a handoff note renews its delivery ID and kills the old ID', async () => {
    let clock = 1_791_000_000_000
    const f = fixture({ now: () => clock })
    const id = 'b'.repeat(24), path = join(f.fleet, 'handoffs', `${id}.md`)
    mkdirSync(join(f.fleet, 'handoffs'))
    writeFileSync(path,
      `HANDOFF from Main to Second - Saturday, October 3, 2026\nTITLE: Offline fixture\nNOTE-ID: ${id}\nSOURCE-KEY: main\nTARGET-KEY: second\nOriginal note.\n`, { mode: 0o600 })
    const issued = await f.tb.callTool('handoff_receipt', { note_id: id, phase: 'issue' })
    if (issued?.content[0]?.type !== 'text') throw Error('Issue receipt missing text')
    const oldId = /Delivery ID: ([a-f0-9]{24})/.exec(issued.content[0].text)?.[1]!
    writeFileSync(path, readFileSync(path, 'utf8').replace('Original note.', 'Edited note.'))
    const editedWithOldId = readFileSync(path, 'utf8')
    expect((await f.tb.callTool('handoff_receipt', { note_id: id, phase: 'sent', delivery_id: oldId }))?.isError).toBe(true)
    const renewed = await f.tb.callTool('handoff_receipt', { note_id: id, phase: 'renew', delivery_id: oldId })
    expect(renewed?.isError).not.toBe(true)
    if (renewed?.content[0]?.type !== 'text') throw Error('Renewed receipt missing text')
    const newId = /Delivery ID: ([a-f0-9]{24})/.exec(renewed.content[0].text)?.[1]!
    expect(newId).toMatch(/^[a-f0-9]{24}$/)
    expect(newId).not.toBe(oldId)
    expect(readFileSync(path, 'utf8')).toContain(`DELIVERY-ID: ${newId}`)
    expect(readFileSync(path, 'utf8')).not.toContain(`DELIVERY-ID: ${oldId}`)
    expect(JSON.parse(readFileSync(join(f.fleet, 'handoffs', 'deliveries', `${oldId}.json`), 'utf8')).phase).toBe('cancelled')
    const renewedNote = readFileSync(path, 'utf8')
    writeFileSync(path, editedWithOldId)
    expect((await f.tb.callTool('handoff_receipt', { note_id: id, phase: 'sent', delivery_id: oldId }))?.isError).toBe(true)
    writeFileSync(path, renewedNote)
    expect((await f.tb.callTool('handoff_receipt', { note_id: id, phase: 'sent', delivery_id: newId }))?.isError).not.toBe(true)
  })
  test('an expired unsent handoff ID can be renewed once', async () => {
    let clock = 1_791_000_000_000
    const f = fixture({ now: () => clock })
    const id = 'c'.repeat(24), path = join(f.fleet, 'handoffs', `${id}.md`)
    mkdirSync(join(f.fleet, 'handoffs'))
    writeFileSync(path,
      `HANDOFF from Main to Second - Saturday, October 3, 2026\nTITLE: Offline fixture\nNOTE-ID: ${id}\nSOURCE-KEY: main\nTARGET-KEY: second\nUnchanged note.\n`, { mode: 0o600 })
    const issued = await f.tb.callTool('handoff_receipt', { note_id: id, phase: 'issue' })
    if (issued?.content[0]?.type !== 'text') throw Error('Issue receipt missing text')
    const oldId = /Delivery ID: ([a-f0-9]{24})/.exec(issued.content[0].text)?.[1]!
    clock += 60 * 60 * 1000 + 1
    expect((await f.tb.callTool('handoff_receipt', { note_id: id, phase: 'sent', delivery_id: oldId }))?.isError).toBe(true)
    const renewed = await f.tb.callTool('handoff_receipt', { note_id: id, phase: 'renew', delivery_id: oldId })
    expect(renewed?.isError).not.toBe(true)
    if (renewed?.content[0]?.type !== 'text') throw Error('Renewed receipt missing text')
    const newId = /Delivery ID: ([a-f0-9]{24})/.exec(renewed.content[0].text)?.[1]!
    expect((await f.tb.callTool('handoff_receipt', { note_id: id, phase: 'sent', delivery_id: newId }))?.isError).not.toBe(true)
  })
  test('source and receiving pollers record one linked handoff note without logging its body', async () => {
    const f = fixture()
    const id = 'a'.repeat(24)
    mkdirSync(join(f.fleet, 'handoffs'))
    writeFileSync(join(f.fleet, 'handoffs', `${id}.md`),
      `HANDOFF from Main to Second - Friday, October 2, 2026\nTITLE: Offline fixture\nRECOMMENDED EFFORT: low\nNOTE-ID: ${id}\nSOURCE-KEY: main\nTARGET-KEY: second\nPrivate note body stays out of events.\n`, { mode: 0o600 })
    expect((await f.tb.callTool('handoff_receipt', { note_id: id, phase: 'received', delivery_id: 'f'.repeat(24) }))?.isError).toBe(true)
    const issued = await f.tb.callTool('handoff_receipt', { note_id: id, phase: 'issue' })
    expect(issued?.isError).not.toBe(true)
    if (issued?.content[0]?.type !== 'text') throw Error('Issue receipt missing text')
    const delivery = /Delivery ID: ([a-f0-9]{24})/.exec(issued.content[0].text)?.[1]
    expect(delivery).toMatch(/^[a-f0-9]{24}$/)
    expect(readFileSync(join(f.fleet, 'handoffs', `${id}.md`), 'utf8')).toContain(`DELIVERY-ID: ${delivery}`)
    expect((await f.tb.callTool('handoff_receipt', { note_id: id, phase: 'sent', delivery_id: delivery }))?.isError).not.toBe(true)
    const second = init({ bot: f.bot, stateDir: f.fleetBots[1]!.state_dir, token: 'offline-test-token', fleetHome: f.fleet,
      mcp: { notification: async () => {} }, loadAccess: () => ({}), assertAllowedChat: () => {} }) as TelebotsModule
    try {
      expect((await second.callTool('handoff_receipt', { note_id: id, phase: 'received', delivery_id: delivery }))?.isError).not.toBe(true)
      expect((await second.callTool('handoff_receipt', { note_id: id, phase: 'received', delivery_id: delivery }))?.isError).toBe(true)
      const received = readFileSync(join(f.fleetBots[1]!.state_dir, 'logs', 'poller-events.log'), 'utf8')
      expect(received).toContain(`HANDOFF-RECEIVED id=${id} source=main target=second delivery=${delivery}`)
      expect(received).not.toContain('Private note body')
      expect(f.events().some(event => event.includes(`HANDOFF-SENT id=${id} source=main target=second delivery=${delivery}`))).toBe(true)
      const checklist = `import importlib.machinery, importlib.util, json, sys\nfrom pathlib import Path\nloader = importlib.machinery.SourceFileLoader('checklist', sys.argv[1])\nspec = importlib.util.spec_from_loader('checklist', loader)\nmodule = importlib.util.module_from_spec(spec)\nloader.exec_module(module)\nreceived = module.log_lines_since(Path(sys.argv[4]) / 'logs/poller-events.log', 0)\nstatus = module.phone_row('P13', Path(sys.argv[2]), Path(sys.argv[3]), 2, False, False, 0)['status']\nprint(json.dumps({'received_lines': len(received), 'status': status}))`
      const checklistResult = JSON.parse(execFileSync('/usr/bin/python3',
        ['-c', checklist, join(process.cwd(), 'engine', 'fleet-checklist'), f.state, f.root, f.fleetBots[1]!.state_dir],
        { env: { ...process.env, FLEET_HOME: f.fleet }, encoding: 'utf8' }))
      expect(checklistResult.received_lines).toBe(1)
      expect(checklistResult.status).toBe('Needs your action')
    } finally { second.dispose() }
  })
  test('handoff dynamically reads the fleet, displays status and excludes self twice', async () => {
    const f = fixture()
    writeFileSync(join(f.fleet, 'fleet.json'), JSON.stringify({ bots: [...f.fleetBots, { key: 'clone', name: 'Clone', state_dir: join(f.root, 'clone'), workdir: f.workdir }, { key: 'new', name: 'New bot', state_dir: join(f.root, 'new'), workdir: join(f.root, 'new-work') }] }))
    f.setOutput(async () => { throw Object.assign(new Error('context unavailable'), { stdout: JSON.stringify([{ key: 'second', up: true, busy: true, context_pct: 14 }, { key: 'new', up: false }]) }) })
    await f.tap('ho:pick')
    const picker = f.lastMessage()
    expect(picker.reply_markup?.inline_keyboard.flat().map(c => c.text)).toEqual(['Second · busy · 14%', 'New bot · down', 'Cancel'])
    await f.tap('ho:new', picker)
    expect(f.notices[0].params).toMatchObject({ content: 'Hand off to New bot', meta: { via: 'button', tapped_message_id: String(picker.message_id) } })
    await f.tap('ho:main')
    expect(f.calls.at(-1)?.payload.text).toBe('That one is not available.')
  })
  test('handoff stale removal, cancellation and single-bot fleets are safe', async () => {
    const f = fixture()
    await f.tap('ho:pick'); const picker = f.lastMessage()
    writeFileSync(join(f.fleet, 'fleet.json'), JSON.stringify({ bots: [f.fleetBots[0]] }))
    await f.tap('ho:second', picker)
    expect(f.notices).toHaveLength(0)
    await f.tap('ho:pick'); expect(f.lastMessage().reply_markup?.inline_keyboard).toEqual([[{ text: 'Cancel', callback_data: 'ho:cancel' }]])
    await f.tap('ho:cancel', f.lastMessage()); expect(f.notices).toHaveLength(0)
  })
  test('resume refresh uses stable bot key, valid UUID exclusion, HTML and bounded output', async () => {
    const f = fixture(), uuid = 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee'
    writeFileSync(join(f.state, 'session.id'), uuid)
    f.setOutput(async () => ({ stdout: JSON.stringify({ text: '<b>Recent chats</b>', count: 1, labels: 1, reply_markup: { inline_keyboard: [[{ text: 'Readable topic', callback_data: `rs:${uuid}` }], [{ text: 'Close', callback_data: 'rsx:close' }]] } }) }))
    await f.tap('rsx:refresh')
    expect(f.runs[0].args).toEqual(['render', '--limit', '20', '--exclude-uuid', uuid, '--bot', 'main', '--style', 'inline'])
    expect(f.runs[0].options.timeout).toBe(8000)
    expect(f.edited()[0].payload.parse_mode).toBe('HTML')
    f.setOutput(async () => ({ stdout: 'invalid' }))
    await f.tap('rsx:refresh')
    expect(f.calls.at(-1)?.payload.text).toBe('Could not refresh the list - try again.')
  })
  test('resume latch blocks simultaneous different messages and releases at 60 seconds', async () => {
    const f = fixture(), data = 'rs:aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee'
    await Promise.all([f.bot.handleUpdate(f.callbackUpdate(data)), f.bot.handleUpdate(f.callbackUpdate(data))]); await f.tb.drain()
    expect(f.spawns).toHaveLength(1)
    expect(f.spawns[0].args).toEqual([data.slice(3), f.workdir, '101'])
    expect(f.scheduled[0].ms).toBe(60_000)
    f.scheduled[0].fn(); await f.tb.drain()
    expect(f.sent().at(-1)?.payload.text).toContain('nothing was restarted')
    await f.tap(data); expect(f.spawns).toHaveLength(2)
  })
  test('resume missing helper releases latch and close has the old-message fallback', async () => {
    const f = fixture(), data = 'rs:aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee'
    rmSync(join(f.fleet, 'bin', 'fleet-resume-open'))
    await f.tap(data); await f.tap(data)
    expect(f.calls.at(-1)?.payload.text).toContain('Resume helper is missing')
    f.setFail(method => method === 'deleteMessage')
    await f.tap('rsx:close')
    expect(f.calls.some(c => c.method === 'editMessageReplyMarkup')).toBe(true)
  })
})

describe('email module routing', () => {
  const email = { to: ['recipient@example.test'], subject: 'Exact subject', body: 'Exact body' }
  test('mail confirmation injects consent through the ordered channel, once', async () => {
    const f = fixture()
    writeFileSync(join(f.fleet, 'email.json'), JSON.stringify({ enabled: true, backend: 'builtin' }))
    expect(f.tb.tools.map(tool => tool.name)).toEqual(['email_preview', 'email_send', 'handoff_receipt'])
    for (const tool of f.tb.tools) expect(tool.description!.length).toBeLessThanOrEqual(2000)
    expect(await f.tb.callTool('unrelated_tool', {})).toBeNull()
    const result = await f.tb.callTool('email_preview', { chat_id: '101', email })
    expect(result?.isError).not.toBe(true)
    let msg = f.lastMessage()
    const prepare = msg.reply_markup!.inline_keyboard.flat().find(cell => 'callback_data' in cell && cell.callback_data.endsWith(':prepare'))!
    const prepareData = (prepare as { callback_data: string }).callback_data
    await f.tap(prepareData, msg)
    expect(f.notices).toHaveLength(0)
    msg = f.lastMessage()
    const confirmData = prepareData.replace(':prepare', ':confirm')
    await f.tap(confirmData, msg)
    await f.tap(confirmData, msg)
    expect(f.notices).toHaveLength(1)
    expect(f.notices[0].params).toMatchObject({ content: 'Confirm send', meta: { via: 'email_confirm', consent_id: prepareData.split(':')[1], tapped_message_id: String(msg.message_id) } })
  })
  test.each(['qr:0:1', 'qr:0:2'])('preview %s tap invalidates its email and remains an ordinary answer', async data => {
    const f = fixture()
    writeFileSync(join(f.fleet, 'email.json'), JSON.stringify({ enabled: true, backend: 'builtin' }))
    await f.tb.callTool('email_preview', { chat_id: '101', email })
    const msg = f.lastMessage(), first = msg.reply_markup!.inline_keyboard.flat()[0] as { callback_data: string }
    await f.tap(data, msg)
    expect(f.notices).toHaveLength(1)
    expect(f.notices[0].params.meta.via).toBe('button')
    await f.tap(first.callback_data, msg)
    const records = JSON.parse(readFileSync(join(f.state, 'consents.json'), 'utf8'))
    expect(records[first.callback_data.split(':')[1]].phase).toBe('cancelled')
  })
  test('external preview edit invalidates consent before stale callbacks', async () => {
    const f = fixture()
    writeFileSync(join(f.fleet, 'email.json'), JSON.stringify({ enabled: true, backend: 'builtin' }))
    await f.tb.callTool('email_preview', { chat_id: '101', email })
    const msg = f.lastMessage(), first = msg.reply_markup!.inline_keyboard.flat()[0] as { callback_data: string }
    f.tb.beforeEdit('101', msg.message_id)
    await f.tap(first.callback_data, msg)
    expect(f.notices).toHaveLength(0)
    const records = JSON.parse(readFileSync(join(f.state, 'consents.json'), 'utf8'))
    expect(records[first.callback_data.split(':')[1]].phase).toBe('cancelled')
  })
})

describe('authorization races and failed keyboard edits', () => {
  test.each(['eff:low', 'me:opus:low', 'rs:aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee', 'qr:0:0'])('%s cannot launch after access is revoked during its awaited edit', async data => {
    const f = fixture()
    f.setBeforeApi(async method => {
      if (method === 'editMessageText') writeFileSync(join(f.state, 'access.json'), JSON.stringify({ dmPolicy: 'allowlist', allowFrom: [] }))
    })
    await f.tap(data, f.message(data, data === 'qr:0:0' ? 'Compact now' : 'Choice'))
    expect(f.spawns).toHaveLength(0)
    expect(f.scheduled).toHaveLength(0)
    expect(f.events().some(event => event.startsWith('SPAWN-DENIED engine='))).toBe(true)
    if (data.startsWith('rs:')) {
      // Denial releases the global resume latch as well as the message lock.
      f.setBeforeApi(async () => {})
      writeFileSync(join(f.state, 'access.json'), JSON.stringify({ dmPolicy: 'allowlist', allowFrom: ['101'] }))
      await f.tap(data)
      expect(f.spawns).toHaveLength(1)
    }
  })
  test.each(['/fresh', '/esc'])('%s rechecks access after its acknowledgment', async command => {
    const f = fixture()
    f.setBeforeApi(async method => {
      if (method === 'sendMessage') writeFileSync(join(f.state, 'access.json'), JSON.stringify({ allowFrom: [] }))
    })
    await f.tb.intercept(f.textContext(), command, '101'); await f.tb.drain()
    expect(f.spawns).toHaveLength(0)
    expect(f.events().some(event => event.startsWith('SPAWN-DENIED engine='))).toBe(true)
  })
  test.each(['qr:0:0', 'ho:second'])('%s cannot inject an answer after revocation during Telegram edit', async data => {
    const f = fixture()
    f.setBeforeApi(async method => {
      if (method === 'editMessageText') writeFileSync(join(f.state, 'access.json'), JSON.stringify({ allowFrom: [] }))
    })
    await f.tap(data)
    expect(f.notices).toHaveLength(0)
    expect(existsSync(join(f.state, 'last-inbound'))).toBe(false)
  })
  test.each(['button', 'reaction', 'email_confirm'])('queued %s is reauthorized when it reaches the head of its chat queue', async via => {
    const f = fixture(), transcription = Promise.withResolvers<{ stdout: string }>()
    let confirmMessage: Message.TextMessage | undefined, confirmData = ''
    if (via === 'email_confirm') {
      writeFileSync(join(f.fleet, 'email.json'), JSON.stringify({ enabled: true, backend: 'builtin' }))
      await f.tb.callTool('email_preview', { chat_id: '101', email: { to: ['owner@example.test'], subject: 'Subject', body: 'Body' } })
      const msg = f.lastMessage(), data = (msg.reply_markup!.inline_keyboard.flat()[0] as { callback_data: string }).callback_data
      await f.tap(data, msg)
      confirmMessage = f.lastMessage(); confirmData = data.replace(':prepare', ':confirm')
    }
    f.setOutput(async () => transcription.promise)
    try {
      await f.tb.enqueueForward(f.textContext(), '(voice)', '101', undefined, { kind: 'voice', file_id: 'offline', duration: 1 })
      if (via === 'button') await f.bot.handleUpdate(f.callbackUpdate('qr:0:0', f.message('qr:0:0', 'Queued answer')))
      else if (via === 'email_confirm') await f.bot.handleUpdate(f.callbackUpdate(confirmData, confirmMessage))
      else await f.bot.handleUpdate({ update_id: ++updateId, message_reaction: { chat: { id: 101, type: 'private', first_name: 'Owner' }, user: { id: 101, is_bot: false, first_name: 'Owner' }, message_id: 55, date: 1, old_reaction: [], new_reaction: [{ type: 'emoji', emoji: '❤' }] } })
      writeFileSync(join(f.state, 'access.json'), JSON.stringify({ allowFrom: [] }))
    } finally { transcription.resolve({ stdout: 'voice was already accepted' }) }
    await f.tb.drain()
    expect(f.notices).toHaveLength(1)
    expect(f.notices[0].params.meta.via).toBeUndefined()
    expect(f.events()).toContain(`FORWARD-DENIED kind=${via}`)
    expect(existsSync(join(f.state, 'last-inbound'))).toBe(false)
  })
  test('failed model edit preserves the old keyboard, blocks overlap, then accepts a retry once', async () => {
    const f = fixture(), msg = f.message('md:opus', 'Opus')
    f.setFail(method => method === 'editMessageText')
    await Promise.all([f.bot.handleUpdate(f.callbackUpdate('md:opus', msg)), f.bot.handleUpdate(f.callbackUpdate('md:opus', msg))])
    await f.tb.drain()
    expect(f.edited()).toHaveLength(1)
    expect(f.events()).toContain('PICKER-FAILED kind=stage2')
    expect(f.events()).not.toContain('PICKER kind=stage2')
    f.setFail(() => false)
    await f.tap('md:opus', msg)
    expect(f.edited()).toHaveLength(2)
    expect(f.events().filter(event => event === 'PICKER kind=stage2')).toHaveLength(1)
    const edit = f.edited().at(-1)!.payload
    await f.tap('md:opus', msg)
    expect(f.edited()).toHaveLength(2)
    await f.tap('me:opus:low', { ...msg, text: String(edit.text), reply_markup: edit.reply_markup } as Message.TextMessage)
    expect(f.spawns).toHaveLength(1)
  })
  test('failed Send it edit preserves the original choices even when entity and plain edits both fail', async () => {
    const f = fixture(), msg = f.message('qr:0:0', '✉️ Send it')
    msg.entities = [{ type: 'bold', offset: 0, length: 8 }]
    f.setFail(method => method === 'editMessageText')
    await Promise.all([f.bot.handleUpdate(f.callbackUpdate('qr:0:0', msg)), f.bot.handleUpdate(f.callbackUpdate('qr:0:0', msg))])
    await f.tb.drain()
    expect(f.edited()).toHaveLength(2)
    expect(f.events()).toContain('SENDIT-SWAP-FAILED')
    expect(f.events()).not.toContain('SENDIT-SWAP')
    expect(f.notices).toHaveLength(0)
    f.setFail(() => false)
    await f.tap('qr:0:0', msg)
    expect(f.edited()).toHaveLength(3)
    expect(f.events().filter(event => event === 'SENDIT-SWAP')).toHaveLength(1)
    await f.tap('qr:0:0', msg)
    expect(f.edited()).toHaveLength(3)
    expect(f.notices).toHaveLength(0)
  })
})

describe('C3 successful UI evidence', () => {
  test('failed key sends never log KEY, and successful sends log it after the API result', async () => {
    const f = fixture()
    f.setFail(method => method === 'sendMessage')
    await f.tb.intercept(f.textContext(), '/key', '101'); await f.tb.drain()
    expect(f.events()).toContain('KEY-ATTEMPT'); expect(f.events()).toContain('KEY-FAILED'); expect(f.events()).not.toContain('KEY')
    f.setFail(() => false)
    f.setBeforeApi(async method => { if (method === 'sendMessage') expect(f.events()).not.toContain('KEY') })
    await f.tb.intercept(f.textContext(), '/key', '101'); await f.tb.drain()
    expect(f.events().filter(event => event === 'KEY')).toHaveLength(1)
  })
  test.each(['model', 'effort', 'handoff'])('%s picker success requires a confirmed send', async kind => {
    const f = fixture()
    const draw = async () => { if (kind === 'handoff') await f.tap('ho:pick'); else { await f.tb.intercept(f.textContext(), `/${kind}`, '101'); await f.tb.drain() } }
    f.setFail(method => method === 'sendMessage')
    await draw()
    expect(f.events()).toContain(`PICKER-FAILED kind=${kind}`)
    expect(f.events()).not.toContain(`PICKER kind=${kind}`)
    expect(f.events().some(event => event.startsWith('HANDOFF-PICKER offered='))).toBe(false)
    f.setFail(() => false)
    await draw()
    expect(f.events().filter(event => event === `PICKER kind=${kind}`)).toHaveLength(1)
  })
  test('resume rendering alone is not picker evidence, and failed edits remain retryable', async () => {
    const f = fixture(), msg = f.message('rsx:refresh')
    f.setOutput(async () => ({ stdout: JSON.stringify({ text: 'Recent chats', count: 1, labels: 1, reply_markup: { inline_keyboard: [[{ text: 'Refresh', callback_data: 'rsx:refresh' }]] } }) }))
    f.setFail(method => method === 'editMessageText')
    await f.tap('rsx:refresh', msg)
    expect(f.events()).toContain('PICKER-FAILED kind=resume')
    expect(f.events().some(event => event.startsWith('PICKER kind=resume'))).toBe(false)
    f.setFail(() => false)
    await f.tap('rsx:refresh', msg)
    expect(f.events()).toContain('PICKER kind=resume count=1 labels=1')
  })
  test.each(['eff:close', 'me:close', 'ho:cancel', 'rsx:close'])('%s failure never logs CLOSE and a later successful retry does', async data => {
    const f = fixture(), msg = f.message(data)
    const kind = data.startsWith('eff:') ? 'effort' : data.startsWith('me:') ? 'stage2' : data.startsWith('ho:') ? 'handoff' : 'resume'
    f.setFail(method => ['editMessageText', 'editMessageReplyMarkup', 'deleteMessage'].includes(method))
    await f.tap(data, msg)
    expect(f.events()).toContain(`CLOSE-FAILED kind=${kind}`)
    expect(f.events()).not.toContain(`CLOSE kind=${kind}`)
    f.setFail(() => false)
    await f.tap(data, msg)
    expect(f.events().filter(event => event === `CLOSE kind=${kind}`)).toHaveLength(1)
    await f.tap(data, msg)
    expect(f.events().filter(event => event === `CLOSE kind=${kind}`)).toHaveLength(1)
  })
})

describe('reactions, ordered forwarding and voice', () => {
  test('only newly added known emoji from allowed private human reactors deliver', async () => {
    const f = fixture()
    const reaction = (changes: Record<string, unknown> = {}): Update => ({ update_id: ++updateId, message_reaction: { chat: { id: 101, type: 'private', first_name: 'Owner' }, user: { id: 101, is_bot: false, first_name: 'Owner' }, message_id: 55, date: 1, old_reaction: [], new_reaction: [{ type: 'emoji', emoji: '❤' }], ...changes } } as Update)
    await f.bot.handleUpdate(reaction()); await f.tb.drain()
    expect(f.notices[0].params).toMatchObject({ content: 'Save everything to my notes', meta: { via: 'reaction', reaction: '❤', tapped_message_id: '55' } })
    for (const changes of [{ old_reaction: [{ type: 'emoji', emoji: '❤' }] }, { new_reaction: [] }, { new_reaction: [{ type: 'custom_emoji', custom_emoji_id: '1' }] }, { user: undefined }, { user: { id: 999, is_bot: false } }, { user: { id: 101, is_bot: true } }, { new_reaction: [{ type: 'emoji', emoji: '🎉' }] }, { chat: { id: -1, type: 'group' } }]) await f.bot.handleUpdate(reaction(changes))
    await f.tb.drain(); expect(f.notices).toHaveLength(1)
    expect(addedReactionEmoji([{ type: 'emoji', emoji: '❤' }], [{ type: 'emoji', emoji: '❤️' } as never, { type: 'emoji', emoji: '👍' }, { type: 'emoji', emoji: '👍' }])).toEqual(['👍'])
  })
  test('voice status ok includes header, duration, caption, path and original attachment metadata', async () => {
    const f = fixture(), attachment: AttachmentMeta = { kind: 'voice', file_id: 'offline', duration: 16, caption: 'a caption', mime: 'audio/ogg', size: 10 }
    await f.tb.enqueueForward(f.textContext(), '(voice message)', '101', undefined, attachment); await f.tb.drain()
    expect(f.notices[0].params.content).toBe('(voice message, 0:16, transcribed on device)\n\nknown words\n\nCaption: a caption')
    expect(f.notices[0].params.meta).toMatchObject({ transcript: 'ok', audio_duration_s: '16', audio_path: join(f.state, 'inbox', 'voice.ogg'), attachment_kind: 'voice', attachment_file_id: 'offline', attachment_mime: 'audio/ogg', attachment_size: '10' })
    expect(f.runs[0].options.timeout).toBe(23_200)
    expect(f.notices[0].params.meta.caption).toBeUndefined()
    expect(f.calls.some(c => c.method === 'setMessageReaction')).toBe(true)
    expect(formatVoiceDuration(3661)).toBe('1:01:01')
  })
  test.each(['empty', 'failed', 'timeout'])('voice status %s keeps the correct fallback', async status => {
    const f = fixture()
    f.setOutput(async () => { if (status === 'empty') return { stdout: '  ' }; throw Object.assign(new Error('offline failure'), { killed: status === 'timeout' }) })
    await f.tb.enqueueForward(f.textContext(), '(audio)', '101', undefined, { kind: 'audio', file_id: 'offline', duration: 900, caption: 'caption' }); await f.tb.drain()
    expect(f.notices[0].params.meta.transcript).toBe(status)
    expect(f.notices[0].params.content).toBe(status === 'empty' ? '(audio file, 15:00, no speech detected)\n\nCaption: caption' : '(audio)')
    expect(f.runs[0].options.timeout).toBe(120_000)
  })
  test('download failure becomes failed; skipped durations and sizes never download', async () => {
    let downloads = 0
    const f = fixture({ downloadFile: async () => { downloads++; throw new Error('offline download failure') } })
    await f.tb.enqueueForward(f.textContext(), 'fallback', '101', undefined, { kind: 'voice', file_id: 'offline', duration: 10 }); await f.tb.drain()
    expect(f.notices[0].params.meta.transcript).toBe('failed')
    for (const attachment of [{ duration: undefined }, { duration: -1 }, { duration: 901 }, { duration: Number.NaN }, { duration: 1, size: 20_000_001 }]) await f.tb.enqueueForward(f.textContext(), 'fallback', '101', undefined, { kind: 'voice', file_id: 'offline', ...attachment })
    await f.tb.drain()
    expect(downloads).toBe(1)
    expect(f.notices.slice(1).every(n => n.params.meta.transcript === 'skipped')).toBe(true)
    writeFileSync(join(f.state, 'autotranscribe.off'), '')
    await f.tb.enqueueForward(f.textContext(), 'old body', '101', undefined, { kind: 'voice', file_id: 'offline', duration: 10 }); await f.tb.drain()
    expect(f.notices.at(-1)?.params.content).toBe('old body')
    expect(f.notices.at(-1)?.params.meta.transcript).toBeUndefined()
  })
  test('voice then text and tap remain ordered, while another chat proceeds', async () => {
    let finish!: (value: { stdout: string }) => void
    const f = fixture()
    f.setOutput(async () => new Promise(yes => { finish = yes }))
    await f.tb.enqueueForward(f.textContext(), '(voice)', '101', undefined, { kind: 'voice', file_id: 'offline', duration: 2 })
    await f.tb.enqueueForward(f.textContext(), 'after voice', '101')
    await f.tb.enqueueForward(f.textContext('other', 202), 'other chat', '202')
    await f.bot.handleUpdate(f.callbackUpdate('qr:0:0', f.message('qr:0:0', 'tap after voice')))
    for (let i = 0; i < 20 && !finish; i++) await Promise.resolve()
    expect(f.notices.map(n => n.params.content)).toEqual(['other chat'])
    finish({ stdout: 'voice words' }); await f.tb.drain()
    expect(f.notices.map(n => n.params.content)).toEqual(['other chat', '(voice message, 0:02, transcribed on device)\n\nvoice words', 'after voice', 'tap after voice'])
  })
  test('one failed delivery does not block the next item and logs contain no message text', async () => {
    const notices: Notice[] = []; let attempt = 0
    const f = fixture({ mcp: { notification: async n => { if (++attempt === 1) throw new Error('do not log this text'); notices.push(n) } } })
    await f.tb.enqueueForward(f.textContext(), 'private message content', '101')
    await f.tb.enqueueForward(f.textContext(), 'next item', '101'); await f.tb.drain()
    expect(notices[0].params.content).toBe('next item')
    const log = readFileSync(join(f.state, 'logs', 'poller-events.log'), 'utf8')
    expect(log).toContain('INBOUND kind=text'); expect(log).toContain('FORWARD-ERROR')
    expect(log).not.toContain('private message content'); expect(log).not.toContain('do not log this text'); expect(log).not.toContain('offline-test-token')
    expect(voiceBody('failed', 'voice', 2, '', undefined, 'fallback')).toBe('fallback')
  })
})
