import type { Bot, Context, InlineKeyboard } from 'grammy'
import type { ReplyKeyboardMarkup, UserFromGetMe } from 'grammy/types'
import type { CallToolResult, Tool } from '@modelcontextprotocol/sdk/types.js'

export type AttachmentMeta = {
  kind: string
  file_id: string
  size?: number
  mime?: string
  name?: string
  duration?: number
  caption?: string
}

export type ForwardParams = {
  chat_id: string
  text: string
  from: { id: number; username?: string }
  message_id?: number
  ts: string
  ackReaction?: string
  downloadImage?: () => Promise<string | undefined>
  attachment?: AttachmentMeta
  via?: 'button' | 'reaction' | 'email_confirm'
  tapped_message_id?: string
  reaction?: string
  consent_id?: string
}

export type EventFields = Record<string, string | number | boolean>
export type ReplyMarkup = InlineKeyboard | ReplyKeyboardMarkup | undefined
export type ExecOptions = { timeout: number; maxBuffer: number }

export type TelebotsDependencies = {
  bot: Bot<Context>
  mcp: { notification(message: { method: string; params: { content: string; meta: Record<string, string> } }): Promise<void> }
  stateDir: string
  token: string
  loadAccess(): { ackReaction?: string }
  assertAllowedChat(chatId: string): void
  fleetHome?: string
  ownerPid?: number | null | (() => number | null)
  apiRoot?: string
  // Injectable operations keep tests offline. Defaults run only after init.
  now?: () => number
  runOutput?: (file: string, args: string[], options: ExecOptions) => Promise<{ stdout: string }>
  spawnDetached?: (file: string, args: string[], logFile: string) => void
  downloadFile?: (fileId: string) => Promise<{ path: string; size: number }>
  schedule?: (callback: () => void, ms: number) => { unref?(): void }
}

export interface Telebots {
  readonly allowedUpdates: Array<'message' | 'callback_query' | 'message_reaction'>
  readonly tools: Tool[]
  registerHandlers(): void
  afterPidWrite(): void
  intercept(ctx: Context, text: string, chatId: string): Promise<boolean>
  enqueueForward(ctx: Context, text: string, chatId: string, downloadImage?: () => Promise<string | undefined>, attachment?: AttachmentMeta): Promise<void>
  replyButtons(raw: unknown): InlineKeyboard | undefined
  replyMarkup(buttons: InlineKeyboard | undefined, index: number, total: number): ReplyMarkup
  replySent(markup: ReplyMarkup): void
  callTool(name: string, args: Record<string, unknown>): Promise<CallToolResult | null>
  beforeEdit(chatId: string, messageId: string | number): void
  onPollStart(info: UserFromGetMe): Promise<void>
  logEvent(event: string, fields?: EventFields): void
  dispose(): void
  // Wait for accepted inbound work in tests and orderly shutdowns.
  drain(): Promise<void>
}
