import { appendFileSync, mkdirSync, realpathSync } from 'node:fs'
import { resolve, sep } from 'node:path'
import type { BotCommand, Message, Update, UserFromGetMe } from 'grammy/types'

// Intentionally invalid outside this fixture. Never accept a caller's token.
export const MOCK_TOKEN = '123:TELEBOTS_TEST_ONLY'
export const MOCK_BOT: UserFromGetMe = {
  id: 123, is_bot: true, first_name: 'Sandbox bot', username: 'sandbox_telebots_bot',
  can_join_groups: true, can_read_all_group_messages: false, supports_inline_queries: false,
  can_connect_to_business: false, has_main_web_app: false,
  has_topics_enabled: false, allows_users_to_create_topics: false,
}
export type MockCall = { method: string; data: Record<string, unknown>; at: number }
type MockMessage = Omit<Message.CommonMessage, 'reply_markup'> & { text?: string; reply_markup?: { inline_keyboard: Array<Array<{ text: string; callback_data: string }>> } }

export function requireSandboxPath(path: string): string {
  const root = process.env.TELEBOTS_SANDBOX_ROOT
  if (process.env.TELEBOTS_TEST !== '1' || !root || process.env.TELEGRAM_BOT_TOKEN) {
    throw new Error('Run poller tests through the scrubbed sandbox wrapper without a token in the environment.')
  }
  const canonicalRoot = realpathSync(root)
  const absolute = resolve(path)
  if (absolute !== canonicalRoot && !absolute.startsWith(canonicalRoot + sep)) {
    throw new Error('Test path escapes the sandbox.')
  }
  // Check existing parents too, so a symlink cannot redirect a later write.
  let current = absolute
  while (true) {
    try {
      const real = realpathSync(current)
      if (real !== canonicalRoot && !real.startsWith(canonicalRoot + sep)) throw new Error('Test path resolves outside sandbox.')
      break
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code !== 'ENOENT') throw error
      current = resolve(current, '..')
    }
  }
  return absolute
}

export class MockTelegram {
  readonly calls: MockCall[] = []
  readonly messages = new Map<string, MockMessage>()
  readonly files = new Map<string, { path: string; body: Uint8Array; mime: string }>()
  readonly apiRoot: string
  readonly logFile: string
  richSupported = true
  pollingError: 401 | 409 | null = null
  allowedUpdates: string[] = []
  commands: BotCommand[] = []
  private server: ReturnType<typeof Bun.serve>
  private updates: Update[] = []
  private nextUpdate = 1
  private nextMessage = 100
  private nextCallback = 1
  private failures = new Map<string, { code: number; remaining: number }>()

  constructor(root: string) {
    const safe = requireSandboxPath(root)
    mkdirSync(safe, { recursive: true, mode: 0o700 })
    this.logFile = resolve(safe, 'telegram-calls.jsonl')
    this.server = Bun.serve({ hostname: '127.0.0.1', port: 0, fetch: request => this.handle(request) })
    this.apiRoot = `http://127.0.0.1:${this.server.port}`
  }

  failNext(method: string, code: number, count = 1): void {
    this.failures.set(method, { code, remaining: count })
  }

  addFile(id: string, body: string | Uint8Array, mime = 'audio/ogg'): void {
    this.files.set(id, { path: `fixtures/${encodeURIComponent(id)}.ogg`, body: typeof body === 'string' ? new TextEncoder().encode(body) : body, mime })
  }

  push(update: Omit<Update, 'update_id'>): number {
    const updateId = this.nextUpdate++
    this.updates.push({ ...update, update_id: updateId } as Update)
    return updateId
  }

  message(chatId: number, text: string, fromId = chatId, type: 'private' | 'group' = 'private'): number {
    const id = this.nextMessage++
    const chat = type === 'private' ? { id: chatId, type, first_name: 'Sandbox owner' } : { id: chatId, type, title: 'Sandbox group' }
    this.push({ message: { message_id: id, date: Math.floor(Date.now() / 1000), chat, from: { id: fromId, is_bot: false, first_name: 'Sandbox owner' }, text } })
    return id
  }

  voice(chatId: number, fileId: string, duration = 3, caption?: string, audio = false): number {
    const id = this.nextMessage++
    const common = { message_id: id, date: Math.floor(Date.now() / 1000), chat: { id: chatId, type: 'private' as const, first_name: 'Sandbox owner' }, from: { id: chatId, is_bot: false, first_name: 'Sandbox owner' }, caption }
    const media = { file_id: fileId, file_unique_id: `unique-${fileId}`, duration, file_size: this.files.get(fileId)?.body.length ?? 12, mime_type: 'audio/ogg' }
    this.push({ message: audio ? { ...common, audio: media } : { ...common, voice: media } })
    return id
  }

  callback(chatId: number, messageId: number, data: string, fromId = chatId, snapshot?: Message): string {
    const message = snapshot ?? this.messages.get(`${chatId}:${messageId}`)
    if (!message) throw new Error('Unknown mock message.')
    const id = `callback-${this.nextCallback++}`
    this.push({ callback_query: { id, from: { id: fromId, is_bot: false, first_name: 'Sandbox owner' }, chat_instance: `chat-${chatId}`, data, message: structuredClone(message) } })
    return id
  }

  reaction(chatId: number, messageId: number, emoji: string, fromId = chatId, remove = false): void {
    const reaction = { type: 'emoji' as const, emoji: emoji as '👍' }
    this.push({ message_reaction: {
      chat: { id: chatId, type: 'private', first_name: 'Sandbox owner' }, message_id: messageId,
      user: { id: fromId, is_bot: false, first_name: 'Sandbox owner' }, date: Math.floor(Date.now() / 1000),
      old_reaction: remove ? [reaction] : [], new_reaction: remove ? [] : [reaction],
    } })
  }

  sent(chatId: number): MockMessage[] {
    return [...this.messages.values()].filter(message => message.chat.id === chatId)
  }

  async waitFor(predicate: () => boolean, description: string, timeout = 5_000): Promise<void> {
    const end = Date.now() + timeout
    while (!predicate()) {
      if (Date.now() > end) throw new Error(`Timed out waiting for ${description}.`)
      await Bun.sleep(10)
    }
  }

  async close(): Promise<void> { await this.server.stop(true) }

  private record(method: string, data: Record<string, unknown>): void {
    const call = { method, data: structuredClone(data), at: Date.now() }
    this.calls.push(call)
    appendFileSync(this.logFile, `${JSON.stringify(call)}\n`, { mode: 0o600 })
  }

  private ok(result: unknown): Response { return Response.json({ ok: true, result }) }
  private error(code: number): Response {
    return Response.json({ ok: false, error_code: code, description: `Sandbox Telegram error ${code}`, ...(code === 429 ? { parameters: { retry_after: 1 } } : {}) }, { status: code })
  }

  private async handle(request: Request): Promise<Response> {
    const url = new URL(request.url)
    if (url.pathname.startsWith(`/file/bot${MOCK_TOKEN}/`)) {
      const path = url.pathname.slice(`/file/bot${MOCK_TOKEN}/`.length)
      this.record('downloadFile', { path })
      const fixture = [...this.files.values()].find(file => file.path === path)
      return fixture ? new Response(new Uint8Array(fixture.body), { headers: { 'content-type': fixture.mime } }) : this.error(404)
    }
    const prefix = `/bot${MOCK_TOKEN}/`
    if (!url.pathname.startsWith(prefix)) return this.error(401)
    const method = url.pathname.slice(prefix.length)
    let data: Record<string, unknown> = {}
    const contentType = request.headers.get('content-type') ?? ''
    if (contentType.includes('application/json')) data = await request.json() as Record<string, unknown>
    else if (contentType.includes('multipart/form-data')) {
      const form = await request.formData()
      for (const [key, value] of form) {
        if (typeof value !== 'string') data[key] = { name: value.name, size: value.size, type: value.type }
        else { try { data[key] = JSON.parse(value) } catch { data[key] = value } }
      }
    } else {
      for (const [key, value] of url.searchParams) data[key] = value
    }
    this.record(method, data)
    const failure = this.failures.get(method)
    if (failure && failure.remaining-- > 0) return this.error(failure.code)
    if (method === 'getUpdates' && this.pollingError) return this.error(this.pollingError)
    if (method === 'sendRichMessage' && !this.richSupported) return this.error(404)
    switch (method) {
      case 'getMe': return this.ok(MOCK_BOT)
      case 'getUpdates': {
        if (Array.isArray(data.allowed_updates)) this.allowedUpdates = data.allowed_updates as string[]
        const offset = Number(data.offset ?? 0)
        this.updates = this.updates.filter(update => update.update_id >= offset)
        if (!this.updates.length) await Bun.sleep(60)
        return this.ok(this.updates.slice(0, 100))
      }
      case 'setMyCommands': this.commands = data.commands as BotCommand[]; return this.ok(true)
      case 'getMyCommands': return this.ok(this.commands)
      case 'getWebhookInfo': return this.ok({ url: '', has_custom_certificate: false, pending_update_count: this.updates.length, allowed_updates: this.allowedUpdates })
      case 'deleteWebhook': case 'sendChatAction': case 'answerCallbackQuery': case 'setMessageReaction': return this.ok(true)
      case 'getFile': {
        const id = String(data.file_id)
        const file = this.files.get(id)
        return file ? this.ok({ file_id: id, file_unique_id: `unique-${id}`, file_size: file.body.length, file_path: file.path }) : this.error(404)
      }
      case 'sendMessage': case 'sendRichMessage': case 'sendDocument': case 'sendPhoto': {
        const chatId = Number(data.chat_id)
        const message = {
          message_id: this.nextMessage++, date: Math.floor(Date.now() / 1000),
          chat: { id: chatId, type: 'private', first_name: 'Sandbox owner' }, from: MOCK_BOT,
          text: typeof data.text === 'string' ? data.text : String(data.caption ?? ''),
          ...(data.reply_markup ? { reply_markup: structuredClone(data.reply_markup) } : {}),
        } as MockMessage
        this.messages.set(`${chatId}:${message.message_id}`, message)
        return this.ok(message)
      }
      case 'editMessageText': case 'editMessageReplyMarkup': {
        const key = `${data.chat_id}:${data.message_id}`
        const message = this.messages.get(key)
        if (!message) return this.error(400)
        if (method === 'editMessageText') message.text = String(data.text)
        message.reply_markup = data.reply_markup ? structuredClone(data.reply_markup) as MockMessage['reply_markup'] : undefined
        return this.ok(message)
      }
      case 'deleteMessage': this.messages.delete(`${data.chat_id}:${data.message_id}`); return this.ok(true)
      default: return this.error(404)
    }
  }
}
