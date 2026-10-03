import { afterEach, describe, expect, setDefaultTimeout, test } from 'bun:test'
import { chmodSync, copyFileSync, cpSync, existsSync, mkdirSync, mkdtempSync, readFileSync, readdirSync, rmSync, statSync, symlinkSync, writeFileSync } from 'node:fs'
import { execFileSync } from 'node:child_process'
import { dirname, join, resolve } from 'node:path'
import { FakeClaudeClient } from './fake-claude.ts'
import { MockTelegram, MOCK_TOKEN, requireSandboxPath } from './mock-telegram.ts'
import ux from '../../poller/ux.json'

// Bun's timeout cleanup sends signals. Own deadlines reject normally, then
// afterEach closes stdin so the isolated poller shuts down without a signal.
setDefaultTimeout(0)

const packageDir = resolve(import.meta.dir, '../../poller')
const owner = 7001
const session = '11111111-2222-4333-8444-555555555555'
type EngineCall = { engine: string; args: string[]; payload?: unknown }

const fixtureEngine = `#!/usr/bin/env python3
import json, os, pathlib, sys, time
root=pathlib.Path(os.environ['FLEET_HOME'])
name=pathlib.Path(sys.argv[0]).name
record={'engine':name,'args':sys.argv[1:]}
if name=='fleet-email':
    record['payload']=json.load(sys.stdin)
with (root/'engine-argv.jsonl').open('a') as f: f.write(json.dumps(record)+'\\n')
if name=='fleet-state':
    fleet=json.loads((root/'fleet.json').read_text())
    print(json.dumps([dict(bot,up=True,busy=False,context_pct=12) for bot in fleet['bots']]))
elif name=='fleet-sessions':
    print(json.dumps({'text':'Recent chats\\n1. Sandbox planning topic', 'reply_markup':{'inline_keyboard':[[{'text':'Sandbox planning topic','callback_data':'rs:${session}'}],[{'text':'Refresh','callback_data':'rsx:refresh'},{'text':'Close','callback_data':'rsx:close'}]]},'count':1,'labels':1}))
elif name=='transcribe-voice':
    mode=(root/'transcriber-mode').read_text() if (root/'transcriber-mode').exists() else 'ok'
    time.sleep(0.15)
    if mode=='failed': sys.exit(1)
    if mode!='empty': print('Sandbox voice transcript.')
elif name=='fleet-email':
    state=pathlib.Path(os.environ['TELEGRAM_STATE_DIR'])
    payload=record['payload']
    consent=json.loads((state/'consents.json').read_text())[payload['consent_id']]
    assert consent['phase']=='consumed'
    attempts=state/'mail-attempts'; attempts.mkdir(exist_ok=True)
    fd=os.open(attempts/payload['consent_id'],os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    os.close(fd)
    print(json.dumps({'ok':True}))
`

class Fixture {
  root: string
  state: string
  fleetHome: string
  workdir: string
  server: string
  mock: MockTelegram
  client: FakeClaudeClient

  constructor(options: { pairing?: boolean; bots?: number; missing?: boolean; broken?: boolean; initError?: boolean; registerError?: boolean; channels?: boolean; staticAccess?: boolean } = {}) {
    const sandbox = requireSandboxPath(process.env.TELEBOTS_SANDBOX_ROOT!)
    this.root = mkdtempSync(join(sandbox, 'integration-'))
    this.state = join(this.root, 'state')
    this.fleetHome = join(this.root, 'fleet')
    this.workdir = join(this.root, 'work', 'main')
    const plugin = join(this.root, 'plugin')
    for (const path of [this.state, this.workdir, plugin, join(this.fleetHome, 'bin')]) mkdirSync(path, { recursive: true, mode: 0o700 })
    const stock = requireSandboxPath(process.env.TELEBOTS_STOCK_SERVER ?? join(sandbox, 'inputs', 'stock-server.ts'))
    this.server = join(plugin, 'server.ts')
    copyFileSync(stock, this.server)
    execFileSync('patch', ['--silent', this.server, join(packageDir, 'seam-0.0.7.patch')], { stdio: 'pipe' })
    for (const file of readdirSync(packageDir).filter(file => file.endsWith('.ts') || file === 'ux.json')) copyFileSync(join(packageDir, file), join(plugin, file))
    const dependencies = join(sandbox, 'inputs', 'node_modules')
    if (!existsSync(dependencies)) cpSync(join(packageDir, 'node_modules'), dependencies, { recursive: true })
    symlinkSync(dependencies, join(plugin, 'node_modules'))
    if (options.missing) rmSync(join(plugin, 'telebots.ts'))
    if (options.broken) writeFileSync(join(plugin, 'telebots.ts'), 'export this is not valid TypeScript\n')
    if (options.initError) writeFileSync(join(plugin, 'telebots.ts'), 'export function init() { throw Error("sandbox init failure") }\n')
    if (options.registerError) writeFileSync(join(plugin, 'telebots.ts'), 'import { writeFileSync } from "node:fs"; export function init({ stateDir }) { return { registerHandlers() { throw Error("sandbox registration failure") }, afterPidWrite() { writeFileSync(stateDir + "/caps", "resume-buttons=1") }, dispose() {} } }\n')
    writeFileSync(join(this.state, '.env'), `TELEGRAM_BOT_TOKEN=${MOCK_TOKEN}\n`, { mode: 0o600 })
    this.access(options.pairing ? [] : [String(owner)], options.pairing ? 'pairing' : 'allowlist')
    writeFileSync(join(this.state, 'session.id'), session)
    writeFileSync(join(this.state, 'model-state'), 'MODEL=opus\nEFFORT=max\n')
    const bots = [{ key: 'main', name: 'Main', emoji: '🤖', state_dir: this.state, workdir: this.workdir }]
    if ((options.bots ?? 2) > 1) bots.push({ key: 'research-lab', name: 'Research Lab', emoji: '🔎', state_dir: join(this.root, 'second-state'), workdir: join(this.root, 'work', 'research') })
    writeFileSync(join(this.fleetHome, 'fleet.json'), JSON.stringify({ version: 2, bots, options: { email: false } }))
    copyFileSync(join(packageDir, 'ux.json'), join(this.fleetHome, 'ux.json'))
    for (const name of ['fleet-fresh', 'fleet-model', 'fleet-update', 'fleet-esc', 'fleet-context', 'fleet-usage', 'fleet-compact', 'fleet-resume-open', 'fleet-state', 'fleet-sessions', 'transcribe-voice', 'fleet-email']) {
      const path = join(this.fleetHome, 'bin', name)
      writeFileSync(path, fixtureEngine, { mode: 0o700 })
    }
    if (options.channels === false) {
      writeFileSync(join(this.state, 'bot.pid'), '999999\n')
      writeFileSync(join(this.state, 'consents.json'), '{"diagnostic":"untouched"}\n', { mode: 0o600 })
    }
    this.mock = new MockTelegram(join(this.root, 'mock'))
    this.client = new FakeClaudeClient({ server: this.server, state: this.state, fleetHome: this.fleetHome, apiRoot: this.mock.apiRoot, root: this.root, channels: options.channels, accessMode: options.staticAccess ? 'static' : undefined })
  }

  async restart(mode: 'normal' | 'missing' = 'normal'): Promise<void> {
    await this.client.close()
    const module = join(dirname(this.server), 'telebots.ts')
    if (mode === 'missing') rmSync(module)
    else copyFileSync(join(packageDir, 'telebots.ts'), module)
    this.client = new FakeClaudeClient({ server: this.server, state: this.state, fleetHome: this.fleetHome, apiRoot: this.mock.apiRoot, root: this.root })
    await this.start()
  }

  access(ids: string[], policy = 'allowlist'): void {
    writeFileSync(join(this.state, 'access.json'), JSON.stringify({ dmPolicy: policy, allowFrom: ids, groups: {}, pending: {}, ackReaction: '👀' }), { mode: 0o600 })
  }
  calls(): EngineCall[] {
    const path = join(this.fleetHome, 'engine-argv.jsonl')
    return existsSync(path) ? readFileSync(path, 'utf8').trim().split('\n').filter(Boolean).map(line => JSON.parse(line) as EngineCall) : []
  }
  events(): string { const path = join(this.state, 'logs', 'poller-events.log'); return existsSync(path) ? readFileSync(path, 'utf8') : '' }
  async start(polling = true): Promise<void> {
    await this.client.initialize()
    if (polling) await this.mock.waitFor(() => this.mock.calls.some(call => call.method === 'getUpdates'), 'poller start')
  }
  async post(method: string, body: Record<string, unknown>): Promise<Record<string, unknown>> {
    const response = await fetch(`${this.mock.apiRoot}/bot${MOCK_TOKEN}/${method}`, { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(body) })
    return (await response.json() as { result: Record<string, unknown> }).result
  }
  async reply(text: string, buttons?: string[]): Promise<number> {
    const result = await this.client.callTool('reply', { chat_id: String(owner), text, ...(buttons ? { buttons } : {}) })
    expect(result.isError).not.toBe(true)
    return this.mock.sent(owner).at(-1)!.message_id
  }
  async waitNotifications(count: number): Promise<void> { await this.mock.waitFor(() => this.client.notifications.filter(note => note.method === 'notifications/claude/channel').length >= count, 'channel notification') }
  channel() { return this.client.notifications.filter(note => note.method === 'notifications/claude/channel') }
  async close(): Promise<void> { try { await this.client.close() } finally { await this.mock.close() } }
}

const fixtures: Fixture[] = []
function fixture(options?: ConstructorParameters<typeof Fixture>[0]): Fixture { const item = new Fixture(options); fixtures.push(item); return item }
afterEach(async () => { while (fixtures.length) await fixtures.shift()!.close() }, 0)

describe('stock seam and mock Telegram with a fake MCP owner', () => {
  test('F2.4 starts the real patched poller, sets 13 commands, heartbeat and capped tools', async () => {
    const f = fixture(); await f.start()
    const tools = await f.client.listTools()
    expect(f.mock.commands).toEqual(ux.commands)
    expect(f.mock.allowedUpdates).toEqual(['message', 'callback_query', 'message_reaction'])
    expect(tools.tools.map(tool => tool.name)).toEqual(['reply', 'react', 'download_attachment', 'edit_message', 'email_preview', 'email_send'])
    expect(f.client.initialized!.instructions!.length).toBeLessThanOrEqual(2000)
    for (const tool of tools.tools) expect(tool.description!.length).toBeLessThanOrEqual(2000)
    await f.mock.waitFor(() => Number(readFileSync(join(f.state, 'poller.heartbeat'), 'utf8')) > 0, 'heartbeat')
    expect(readFileSync(join(f.state, 'caps'), 'utf8')).toContain('resume-buttons=1')
    expect(f.events()).toContain('MENU-SET n=13')
    await f.mock.waitFor(() => f.events().includes('POLL-OK'), 'completed successful poll')
    expect(readFileSync(join(f.state, 'logs', 'poller-starts.log'), 'utf8')).toContain('disabled=false')
  })

  test('F2.5 retains stock pairing and approval semantics', async () => {
    const f = fixture({ pairing: true }); await f.start()
    f.mock.message(owner, 'Sandbox pairing request')
    await f.mock.waitFor(() => f.mock.sent(owner).some(message => message.text?.includes('Pairing required')), 'pairing prompt')
    const path = join(f.state, 'access.json')
    const access = JSON.parse(readFileSync(path, 'utf8'))
    const [code, pending] = Object.entries(access.pending)[0]! as [string, { senderId: string; chatId: string; expiresAt: number }]
    expect(code).toMatch(/^[a-f0-9]{6}$/)
    expect(pending.senderId).toBe(String(owner))
    expect(pending.expiresAt).toBeGreaterThan(Date.now())
    access.allowFrom.push(pending.senderId); delete access.pending[code]; access.dmPolicy = 'allowlist'
    writeFileSync(path, JSON.stringify(access), { mode: 0o600 })
    mkdirSync(join(f.state, 'approved'), { recursive: true })
    writeFileSync(join(f.state, 'approved', pending.senderId), pending.chatId)
    writeFileSync(join(f.state, '.enrolled'), '')
    await f.mock.waitFor(() => f.mock.sent(owner).some(message => message.text?.includes('Paired!')), 'stock approval reply', 7000)
    f.mock.message(owner, 'Sandbox paired message'); await f.waitNotifications(1)
    expect(f.channel()[0]!.params.content).toBe('Sandbox paired message')
  })

  test('F2.6-7 delivers text, uses one quick bar, consumes taps, and never executes a slash label', async () => {
    const f = fixture(); await f.start()
    f.mock.message(owner, 'Sandbox text'); await f.waitNotifications(1)
    expect(f.channel()[0]!.params.meta.chat_id).toBe(String(owner))
    await f.reply('First plain reply'); await f.reply('Second plain reply')
    const sends = f.mock.calls.filter(call => call.method === 'sendMessage')
    expect(sends.filter(call => (call.data.reply_markup as { keyboard?: unknown })?.keyboard).length).toBe(1)
    const id = await f.reply('Pick one', ['Alpha', 'Beta', '/fresh'])
    const original = structuredClone(f.mock.messages.get(`${owner}:${id}`)!)
    expect(original.reply_markup!.inline_keyboard.map(row => row.length)).toEqual([3])
    f.mock.callback(owner, id, 'qr:0:2'); await f.waitNotifications(2)
    expect(f.channel()[1]!.params.content).toBe('/fresh')
    expect(f.channel()[1]!.params.meta.via).toBe('button')
    expect(f.channel()[1]!.params.meta.tapped_message_id).toBe(String(id))
    expect(f.calls()).toEqual([])
    expect(f.mock.messages.get(`${owner}:${id}`)!.text).toContain('▸ /fresh')
    f.mock.callback(owner, id, 'qr:0:2', owner, original)
    await f.mock.waitFor(() => f.mock.calls.some(call => call.method === 'answerCallbackQuery' && call.data.text === 'Already answered.'), 'stale callback refusal')
    expect(f.channel()).toHaveLength(2)
    const medium = await f.reply('Medium labels', ['123456789012345', 'Beta', 'Gamma'])
    expect(f.mock.messages.get(`${owner}:${medium}`)!.reply_markup!.inline_keyboard[0]!.length).toBe(2)
    const long = await f.reply('Long labels', ['A label that should occupy the whole row', 'Beta'])
    expect(f.mock.messages.get(`${owner}:${long}`)!.reply_markup!.inline_keyboard[0]!.length).toBe(1)
  })

  test('F2.7 checks private chat and the fresh disk allowlist for every callback', async () => {
    const f = fixture({ staticAccess: true }); await f.start()
    const id = await f.reply('Authorized choice', ['Yes'])
    f.mock.callback(owner, id, 'qr:0:0', 8008)
    await f.mock.waitFor(() => f.mock.calls.some(call => call.data.text === 'Not authorized.'), 'unauthorized refusal')
    f.access([])
    f.mock.callback(owner, id, 'qr:0:0')
    await f.mock.waitFor(() => f.mock.calls.filter(call => call.data.text === 'Not authorized.').length >= 2, 'disk-fresh refusal in static mode')
    f.access([String(owner)])
    const group = structuredClone(f.mock.messages.get(`${owner}:${id}`)!)
    Object.assign(group.chat, { id: -100, type: 'group', title: 'Sandbox group' })
    f.mock.callback(owner, id, 'qr:0:0', owner, group)
    await f.mock.waitFor(() => f.mock.calls.some(call => call.data.text === 'Only works in a direct message.'), 'group refusal')
    expect(f.channel()).toHaveLength(0)
  })

  test('F2.8-9 swaps Send it without consent and maps only added allowlisted reactions', async () => {
    const f = fixture(); await f.start()
    const id = await f.reply('Handoff preview fixture', ['✉️ Send it', '✏️ Edit'])
    f.mock.callback(owner, id, 'qr:0:0')
    await f.mock.waitFor(() => f.events().includes('SENDIT-SWAP'), 'two-tap swap')
    await f.mock.waitFor(() => f.mock.messages.get(`${owner}:${id}`)?.reply_markup?.inline_keyboard[0]?.[0]?.text === '✖ Cancel', 'Cancel first')
    expect(f.mock.messages.get(`${owner}:${id}`)!.reply_markup!.inline_keyboard[0]!.map(cell => cell.text)).toEqual(['✖ Cancel', '✅ Confirm send'])
    expect(f.channel()).toHaveLength(0)
    expect(existsSync(join(f.state, 'consents.json'))).toBe(false)
    f.mock.reaction(owner, id, '❤️'); await f.waitNotifications(1)
    expect(f.channel()[0]!.params.content).toBe(ux.reactions[0]!.phrase)
    expect(f.channel()[0]!.params.meta.via).toBe('reaction')
    f.mock.reaction(owner, id, '❤', owner, true)
    f.mock.reaction(owner, id, '👍', 8008)
    f.mock.reaction(owner, id, '🎉')
    f.mock.push({ message_reaction: { chat: { id: owner, type: 'private', first_name: 'Sandbox owner' }, message_id: id, user: { id: owner, is_bot: false, first_name: 'Sandbox owner' }, date: Math.floor(Date.now()/1000), old_reaction: [], new_reaction: [{ type: 'custom_emoji', custom_emoji_id: 'test-custom' }] } })
    f.mock.message(owner, 'reaction barrier'); await f.waitNotifications(2)
    expect(f.channel()).toHaveLength(2)
  })

  test('F2.10 renders the shared legend and disconnected inbox hint', async () => {
    const f = fixture(); await f.start()
    f.mock.message(owner, '/key'); f.mock.message(owner, '/inbox')
    await f.mock.waitFor(() => f.mock.sent(owner).length >= 2, 'key and inbox replies')
    const key = f.mock.sent(owner).find(message => message.text?.includes(ux.reactions[0]!.key_line))!.text!
    for (const reaction of ux.reactions) expect(key).toContain(reaction.key_line)
    for (const command of ux.commands) expect(key).toContain('/' + command.command)
    expect(f.mock.sent(owner).some(message => message.text === 'Inbox is not connected. On your Mac run: fleet-email setup')).toBe(true)
    expect(f.channel()).toHaveLength(0)
  })

  test('F2.11 walks model then effort and every model/effort Close', async () => {
    const f = fixture(); await f.start()
    f.mock.message(owner, '/model')
    await f.mock.waitFor(() => f.mock.sent(owner).length === 1, 'model picker')
    const id = f.mock.sent(owner)[0]!.message_id
    f.mock.callback(owner, id, 'md:opus')
    await f.mock.waitFor(() => f.mock.messages.get(`${owner}:${id}`)!.reply_markup!.inline_keyboard.some(row => row.some(cell => cell.callback_data === 'me:opus:low')), 'stage two')
    f.mock.callback(owner, id, 'me:opus:low')
    await f.mock.waitFor(() => f.calls().some(call => call.engine === 'fleet-model'), 'model engine')
    expect(f.calls().find(call => call.engine === 'fleet-model')!.args).toEqual([f.workdir, String(owner), 'opus low'])
    for (const cmd of ['/model', '/effort']) {
      const count = f.mock.sent(owner).length; f.mock.message(owner, cmd)
      await f.mock.waitFor(() => f.mock.sent(owner).length > count, `${cmd} picker`)
      const picker = f.mock.sent(owner).at(-1)!
      f.mock.callback(owner, picker.message_id, 'eff:close')
      await f.mock.waitFor(() => f.mock.messages.get(`${owner}:${picker.message_id}`)!.text?.includes('closed') === true, `${cmd} close`)
    }
    const count = f.mock.sent(owner).length; f.mock.message(owner, '/model')
    await f.mock.waitFor(() => f.mock.sent(owner).length > count, 'second model picker')
    const stage = f.mock.sent(owner).at(-1)!
    f.mock.callback(owner, stage.message_id, 'md:sonnet')
    await f.mock.waitFor(() => f.mock.messages.get(`${owner}:${stage.message_id}`)!.text?.includes('Now pick') === true, 'second stage')
    f.mock.callback(owner, stage.message_id, 'me:close')
    await f.mock.waitFor(() => f.mock.messages.get(`${owner}:${stage.message_id}`)!.text === 'Model picker closed.', 'stage two close')
    f.mock.message(owner, '/effort low')
    await f.mock.waitFor(() => f.calls().filter(call => call.engine === 'fleet-model').length === 2, 'direct effort engine')
    expect(f.calls().at(-1)!.args).toEqual([f.workdir, String(owner), 'low'])
  })

  test('F2.12 lists configured peers, excludes self, cancels and injects a named handoff', async () => {
    const f = fixture(); await f.start()
    const nudge = await f.post('sendMessage', { chat_id: owner, text: 'Sandbox context nudge', reply_markup: { inline_keyboard: [[{ text: 'Hand off', callback_data: 'ho:pick' }]] } })
    const id = Number(nudge.message_id); f.mock.callback(owner, id, 'ho:pick')
    await f.mock.waitFor(() => f.events().includes('HANDOFF-PICKER'), 'handoff picker')
    await f.mock.waitFor(() => f.mock.sent(owner).some(message => message.reply_markup?.inline_keyboard.some(row => row.some(cell => cell.callback_data === 'ho:research-lab'))), 'peer button')
    const picker = f.mock.sent(owner).at(-1)!
    const choices = picker.reply_markup!.inline_keyboard.flat()
    expect(choices.some(cell => cell.callback_data === 'ho:main')).toBe(false)
    f.mock.callback(owner, picker.message_id, 'ho:cancel')
    await f.mock.waitFor(() => f.mock.messages.get(`${owner}:${picker.message_id}`)!.text === 'Handoff cancelled.', 'handoff cancel')
    expect(f.channel()).toHaveLength(0)
    const second = await f.post('sendMessage', { chat_id: owner, text: 'Sandbox context nudge again', reply_markup: { inline_keyboard: [[{ text: 'Hand off', callback_data: 'ho:pick' }]] } })
    f.mock.callback(owner, Number(second.message_id), 'ho:pick')
    await f.mock.waitFor(() => f.mock.sent(owner).some(message => message.reply_markup?.inline_keyboard.some(row => row.some(cell => cell.callback_data === 'ho:research-lab'))), 'second peer button')
    f.mock.callback(owner, f.mock.sent(owner).at(-1)!.message_id, 'ho:research-lab'); await f.waitNotifications(1)
    expect(f.channel()[0]!.params.content).toBe('Hand off to Research Lab')
  })

  test('F2.13 spawns every intercept with exact argv and supports resume refresh/close/open', async () => {
    const f = fixture(); await f.start()
    for (const command of ['/fresh', '/update', '/esc', '/compact focus\nsecond\tline', '/context', '/usage', '/opus', '/fable', '/model sonnet high']) f.mock.message(owner, command)
    await f.mock.waitFor(() => f.calls().length >= 9, 'all intercept engines')
    const calls = f.calls()
    const pid = calls.find(call => call.engine === 'fleet-fresh')!.args[0]!
    expect(Number(pid)).toBeGreaterThan(1)
    expect(calls.find(call => call.engine === 'fleet-fresh')!.args).toEqual([pid, f.workdir, 'fresh-reset'])
    expect(calls.find(call => call.engine === 'fleet-update')!.args).toEqual([pid, f.workdir, String(owner)])
    expect(calls.find(call => call.engine === 'fleet-esc')!.args).toEqual([pid, f.workdir, String(owner)])
    expect(calls.find(call => call.engine === 'fleet-compact')!.args).toEqual([pid, f.workdir, 'focus second line'])
    for (const engine of ['fleet-context', 'fleet-usage']) expect(calls.find(call => call.engine === engine)!.args).toEqual([f.workdir, String(owner)])
    const markup = { inline_keyboard: [[{ text: 'Sandbox topic', callback_data: `rs:${session}` }], [{ text: 'Refresh', callback_data: 'rsx:refresh' }, { text: 'Close', callback_data: 'rsx:close' }]] }
    const preview = await f.post('sendMessage', { chat_id: owner, text: 'Recent chats fixture', reply_markup: markup })
    const id = Number(preview.message_id); f.mock.callback(owner, id, 'rsx:refresh')
    await f.mock.waitFor(() => f.calls().some(call => call.engine === 'fleet-sessions'), 'resume renderer')
    await f.mock.waitFor(() => f.mock.messages.get(`${owner}:${id}`)!.text?.includes('Sandbox planning topic') === true, 'refreshed session labels')
    expect(f.calls().find(call => call.engine === 'fleet-sessions')!.args).toEqual(['render', '--limit', '20', '--exclude-uuid', session, '--bot', 'main', '--style', 'inline'])
    f.mock.callback(owner, id, 'rsx:close')
    await f.mock.waitFor(() => f.events().includes('CLOSE kind=resume'), 'resume close')
    const again = await f.post('sendMessage', { chat_id: owner, text: 'Recent chats fixture', reply_markup: markup })
    f.mock.callback(owner, Number(again.message_id), `rs:${session}`)
    await f.mock.waitFor(() => f.calls().some(call => call.engine === 'fleet-resume-open'), 'resume open')
    expect(f.calls().find(call => call.engine === 'fleet-resume-open')!.args).toEqual([session, f.workdir, String(owner)])
  })

  test('F2.14 downloads and transcribes voice, preserves ordering, and exposes fallback statuses', async () => {
    const f = fixture(); await f.start(); f.mock.addFile('voice-note', 'fixture audio')
    f.mock.voice(owner, 'voice-note', 65, 'Fixture caption'); f.mock.message(owner, 'After voice')
    await f.waitNotifications(2)
    expect(f.channel()[0]!.params.content).toContain('1:05, transcribed on device')
    expect(f.channel()[0]!.params.content).toContain('Sandbox voice transcript.')
    expect(f.channel()[0]!.params.meta.transcript).toBe('ok')
    expect(f.channel()[1]!.params.content).toBe('After voice')
    const alias = await f.client.callTool('download_attachment', { attachment_file_id: 'voice-note' })
    expect(alias.isError).not.toBe(true)
    writeFileSync(join(f.state, 'autotranscribe.off'), '')
    f.mock.voice(owner, 'voice-note'); await f.waitNotifications(3)
    expect(f.channel()[2]!.params.meta.transcript).toBeUndefined()
    expect(f.channel()[2]!.params.content).toBe('(voice message)')
    rmSync(join(f.state, 'autotranscribe.off'))
    writeFileSync(join(f.fleetHome, 'transcriber-mode'), 'failed')
    f.mock.voice(owner, 'voice-note'); await f.waitNotifications(4)
    expect(f.channel()[3]!.params.meta.transcript).toBe('failed')
    writeFileSync(join(f.fleetHome, 'transcriber-mode'), 'empty')
    f.mock.voice(owner, 'voice-note', 2, undefined, true); await f.waitNotifications(5)
    expect(f.channel()[4]!.params.meta.transcript).toBe('empty')
    f.mock.voice(owner, 'voice-note', 901); await f.waitNotifications(6)
    expect(f.channel()[5]!.params.meta.transcript).toBe('skipped')
    expect(readFileSync(join(f.state, 'logs', 'voice-transcribe.log'), 'utf8')).toContain('status=ok')
  })

  test('F2.15 requires exact email and two taps, spends consent once, and invalidates edits', async () => {
    const f = fixture(); await f.start()
    writeFileSync(join(f.fleetHome, 'email.json'), JSON.stringify({ enabled: true, backend: 'builtin' }))
    const email = { to: ['recipient@example.test'], cc: ['copy@example.test'], bcc: ['blind@example.test'], subject: 'Sandbox email', body: 'Fixture body only.' }
    const result = await f.client.callTool('email_preview', { chat_id: String(owner), email })
    expect(result.isError).not.toBe(true)
    const preview = f.mock.sent(owner).at(-1)!, id = preview.message_id
    const consentId = Object.keys(JSON.parse(readFileSync(join(f.state, 'consents.json'), 'utf8')))[0]!
    expect(preview.text).toContain('Bcc: blind@example.test')
    const early = await f.client.callTool('email_send', { chat_id: String(owner), consent_id: consentId, email })
    expect(early.isError).toBe(true)
    f.mock.callback(owner, id, `mail:${consentId}:prepare`)
    await f.mock.waitFor(() => f.mock.messages.get(`${owner}:${id}`)!.reply_markup!.inline_keyboard[0]![0]!.text === '✖ Cancel', 'email prepare swap')
    expect(f.channel()).toHaveLength(0)
    expect((await f.client.callTool('email_send', { chat_id: String(owner), consent_id: consentId, email })).isError).toBe(true)
    f.mock.callback(owner, id, `mail:${consentId}:confirm`); await f.waitNotifications(1)
    expect(f.channel()[0]!.params.meta.via).toBe('email_confirm')
    expect(f.channel()[0]!.params.meta.consent_id).toBe(consentId)
    expect((await f.client.callTool('email_send', { chat_id: String(owner), consent_id: consentId, email: { ...email, to: ['changed@example.test'] } })).isError).toBe(true)
    expect((await f.client.callTool('email_send', { chat_id: String(owner), consent_id: consentId, email })).isError).not.toBe(true)
    expect((await f.client.callTool('email_send', { chat_id: String(owner), consent_id: consentId, email })).isError).toBe(true)
    expect(f.calls().filter(call => call.engine === 'fleet-email')).toHaveLength(1)
    expect(statSync(join(f.state, 'consents.json')).mode & 0o777).toBe(0o600)
    expect(readdirSync(join(f.state, 'mail-attempts'))).toEqual([consentId])
    await f.client.callTool('email_preview', { chat_id: String(owner), email })
    const edited = f.mock.sent(owner).at(-1)!
    const before = JSON.parse(readFileSync(join(f.state, 'consents.json'), 'utf8'))
    const editConsent = Object.keys(before).find(key => key !== consentId)!
    await f.client.callTool('edit_message', { chat_id: String(owner), message_id: String(edited.message_id), text: 'Edited fixture preview' })
    expect(JSON.parse(readFileSync(join(f.state, 'consents.json'), 'utf8'))[editConsent].phase).toBe('cancelled')
    await f.client.callTool('email_preview', { chat_id: String(owner), email })
    const expiring = f.mock.sent(owner).at(-1)!
    const records = JSON.parse(readFileSync(join(f.state, 'consents.json'), 'utf8'))
    const expiredId = Object.keys(records).find(key => records[key].message === expiring.message_id)!
    records[expiredId].expires = Date.now() - 1
    writeFileSync(join(f.state, 'consents.json'), JSON.stringify(records), { mode: 0o600 })
    const expiredPress = f.mock.callback(owner, expiring.message_id, `mail:${expiredId}:prepare`)
    await f.mock.waitFor(() => f.mock.calls.some(call => call.method === 'answerCallbackQuery' && call.data.callback_query_id === expiredPress && call.data.text === 'This email confirmation is unavailable. Request a fresh preview.'), 'expired email refusal')
    expect((await f.client.callTool('email_send', { chat_id: String(owner), consent_id: expiredId, email })).isError).toBe(true)
    expect(f.calls().filter(call => call.engine === 'fleet-email')).toHaveLength(1)
  })

  test('email consent cannot survive an edit through stock fallback between poller instances', async () => {
    const f = fixture(); await f.start()
    writeFileSync(join(f.fleetHome, 'email.json'), JSON.stringify({ enabled: true, backend: 'builtin' }))
    const email = { to: ['recipient@example.test'], subject: 'Restart fixture', body: 'Requires fresh approval after restart.' }
    await f.client.callTool('email_preview', { chat_id: String(owner), email })
    const preview = f.mock.sent(owner).at(-1)!
    const consentId = Object.keys(JSON.parse(readFileSync(join(f.state, 'consents.json'), 'utf8')))[0]!
    f.mock.callback(owner, preview.message_id, `mail:${consentId}:prepare`)
    await f.mock.waitFor(() => preview.reply_markup?.inline_keyboard[0]?.[0]?.text === '✖ Cancel', 'prepare before restart')
    f.mock.callback(owner, preview.message_id, `mail:${consentId}:confirm`); await f.waitNotifications(1)
    await f.restart('missing')
    expect((await f.client.listTools()).tools).toHaveLength(4)
    await f.client.callTool('edit_message', { chat_id: String(owner), message_id: String(preview.message_id), text: 'Changed while only stock was loaded' })
    await f.restart()
    expect(JSON.parse(readFileSync(join(f.state, 'consents.json'), 'utf8'))[consentId].phase).toBe('cancelled')
    expect((await f.client.callTool('email_send', { chat_id: String(owner), consent_id: consentId, email })).isError).toBe(true)
    expect(f.calls().filter(call => call.engine === 'fleet-email')).toHaveLength(0)
  })

  for (const mode of ['missing', 'broken', 'initError', 'registerError'] as const) test(`stock forwarding and heartbeat survive a ${mode} module`, async () => {
    const f = fixture({ [mode]: true }); await f.start()
    expect((await f.client.listTools()).tools).toHaveLength(4)
    expect(f.mock.commands.map(command => command.command)).toEqual(['start', 'help', 'status'])
    f.mock.message(owner, 'Stock fallback fixture'); await f.waitNotifications(1)
    expect(f.channel()[0]!.params.content).toBe('Stock fallback fixture')
    await f.reply('Stock outbound fixture')
    expect(Number(readFileSync(join(f.state, 'poller.heartbeat'), 'utf8'))).toBeGreaterThan(0)
    expect(f.client.stderr).toContain('Telebots')
    expect(existsSync(join(f.state, 'caps'))).toBe(false)
  })

  test('nearest nonchannel claude ancestor serves MCP without polling or owning the PID', async () => {
    const f = fixture({ channels: false }); await f.start(false)
    expect((await f.client.listTools()).tools).toHaveLength(4)
    await f.reply('Tools remain available')
    expect(readFileSync(join(f.state, 'logs', 'poller-starts.log'), 'utf8')).toContain('disabled=true')
    expect(readFileSync(join(f.state, 'bot.pid'), 'utf8')).toBe('999999\n')
    expect(f.mock.calls.some(call => call.method === 'getUpdates')).toBe(false)
    expect(existsSync(join(f.state, 'poller.heartbeat'))).toBe(false)
    expect(readFileSync(join(f.state, 'consents.json'), 'utf8')).toBe('{"diagnostic":"untouched"}\n')
    expect(f.client.stderr).not.toContain('Telebots unavailable')
  })
})
