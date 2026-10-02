import { afterEach, expect, setDefaultTimeout, test } from 'bun:test'
import { mkdtempSync, readFileSync, rmSync } from 'node:fs'
import { join } from 'node:path'
import { MockTelegram, MOCK_TOKEN, requireSandboxPath } from './mock-telegram.ts'

setDefaultTimeout(0)
const fixtures: Array<{ root: string; api: MockTelegram }> = []
function fixture(): MockTelegram {
  const root = mkdtempSync(join(requireSandboxPath(process.env.TELEBOTS_SANDBOX_ROOT!), 'mock-unit-'))
  const api = new MockTelegram(root)
  fixtures.push({ root, api })
  return api
}
async function call(api: MockTelegram, method: string, data: Record<string, unknown> = {}): Promise<{ status: number; body: any }> {
  const response = await fetch(`${api.apiRoot}/bot${MOCK_TOKEN}/${method}`, {
    method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(data),
  })
  return { status: response.status, body: await response.json() }
}
afterEach(async () => {
  for (const { root, api } of fixtures.splice(0)) {
    await api.close()
    rmSync(root, { recursive: true, force: true })
  }
})

test('mock serves menu, queued updates, message edits, callbacks and deletes', async () => {
  const api = fixture()
  expect(api.apiRoot).toMatch(/^http:\/\/127\.0\.0\.1:[0-9]+$/)
  expect((await call(api, 'getMe')).body.result.is_bot).toBeTrue()
  const commands = [{ command: 'key', description: 'Show the key' }]
  expect((await call(api, 'setMyCommands', { commands })).body.result).toBeTrue()
  expect((await call(api, 'getMyCommands')).body.result).toEqual(commands)
  api.message(7001, 'A sandbox message')
  const updates = (await call(api, 'getUpdates', { allowed_updates: ['message'] })).body.result
  expect(updates).toHaveLength(1)
  expect((await call(api, 'getUpdates', { offset: updates[0].update_id + 1 })).body.result).toEqual([])
  expect(api.allowedUpdates).toEqual(['message'])
  const sent = (await call(api, 'sendMessage', { chat_id: 7001, text: 'Question' })).body.result
  const target = { chat_id: 7001, message_id: sent.message_id }
  const reply_markup = { inline_keyboard: [[{ text: 'Yes', callback_data: 'qr:0:0' }]] }
  expect((await call(api, 'editMessageText', { ...target, text: 'Revised', reply_markup })).body.result.text).toBe('Revised')
  api.callback(7001, sent.message_id, 'qr:0:0')
  expect((await call(api, 'getUpdates')).body.result[0].callback_query.data).toBe('qr:0:0')
  expect((await call(api, 'editMessageReplyMarkup', { ...target, reply_markup: { inline_keyboard: [] } })).body.result.reply_markup.inline_keyboard).toEqual([])
  for (const method of ['answerCallbackQuery', 'setMessageReaction', 'sendChatAction', 'deleteWebhook']) {
    expect((await call(api, method, target)).body.result).toBeTrue()
  }
  expect((await call(api, 'getWebhookInfo')).body.result.url).toBe('')
  expect((await call(api, 'deleteMessage', target)).body.result).toBeTrue()
  expect(api.sent(7001)).toEqual([])
})

test('mock supports file downloads and uploads without logging token URLs or file bytes', async () => {
  const api = fixture()
  api.addFile('voice-1', 'sandbox audio bytes')
  const file = (await call(api, 'getFile', { file_id: 'voice-1' })).body.result
  const response = await fetch(`${api.apiRoot}/file/bot${MOCK_TOKEN}/${file.file_path}`)
  expect(await response.text()).toBe('sandbox audio bytes')
  for (const method of ['sendDocument', 'sendPhoto']) {
    const form = new FormData()
    form.set('chat_id', '7001')
    form.set(method === 'sendDocument' ? 'document' : 'photo', new File(['private fixture bytes'], 'fixture.txt'))
    const result = await fetch(`${api.apiRoot}/bot${MOCK_TOKEN}/${method}`, { method: 'POST', body: form })
    expect(result.status).toBe(200)
  }
  expect((await call(api, 'sendRichMessage', { chat_id: 7001, text: 'Rich fixture' })).body.ok).toBeTrue()
  const log = readFileSync(api.logFile, 'utf8')
  expect(log).not.toContain(MOCK_TOKEN)
  expect(log).not.toContain('private fixture bytes')
  expect(log).toContain('downloadFile')
  expect(log).toContain('fixture.txt')
})

test('mock exercises rich fallback, flood retry parameters, unauthorized and conflict modes', async () => {
  const api = fixture()
  api.richSupported = false
  expect((await call(api, 'sendRichMessage')).status).toBe(404)
  api.failNext('sendMessage', 429)
  const throttled = await call(api, 'sendMessage', { chat_id: 7001, text: 'Retry fixture' })
  expect(throttled.status).toBe(429)
  expect(throttled.body.parameters.retry_after).toBe(1)
  expect((await call(api, 'sendMessage', { chat_id: 7001, text: 'Retry fixture' })).status).toBe(200)
  api.pollingError = 409
  expect((await call(api, 'getUpdates')).status).toBe(409)
  api.pollingError = 401
  expect((await call(api, 'getUpdates')).status).toBe(401)
  api.pollingError = null
  expect((await call(api, 'getUpdates')).status).toBe(200)
  expect((await fetch(`${api.apiRoot}/botinvalid/getMe`)).status).toBe(401)
})
