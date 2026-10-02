import { mkdirSync, symlinkSync, existsSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'
import type { CallToolResult, InitializeResult, ListToolsResult } from '@modelcontextprotocol/sdk/types.js'
import { requireSandboxPath } from './mock-telegram.ts'

export type ChannelNotification = { method: string; params: { content: string; meta: Record<string, string> } }
type Pending = { resolve(value: unknown): void; reject(error: Error): void; timer: ReturnType<typeof setTimeout> }
type ClientOptions = { server: string; state: string; fleetHome: string; apiRoot: string; root: string; channels?: boolean; accessMode?: 'static' }

// This fixture launches Bun under the executable name claude. It never starts
// Claude Code and never sends a process signal, including during cleanup.
export class FakeClaudeClient {
  readonly notifications: ChannelNotification[] = []
  stderr = ''
  initialized?: InitializeResult
  private child: Bun.Subprocess<'pipe', 'pipe', 'pipe'>
  private sequence = 0
  private pending = new Map<number, Pending>()
  private reading: Promise<void>
  private readingErrors: Promise<void>

  constructor(options: ClientOptions) {
    const root = requireSandboxPath(options.root)
    for (const path of [options.server, options.state, options.fleetHome]) requireSandboxPath(path)
    if (!/^http:\/\/127\.0\.0\.1:[0-9]+$/.test(options.apiRoot)) throw new Error('Fake client requires the loopback mock API.')
    const bin = join(root, 'fake-bin')
    mkdirSync(bin, { recursive: true, mode: 0o700 })
    const executable = join(bin, 'claude')
    if (!existsSync(executable)) symlinkSync(process.execPath, executable)
    const env: Record<string, string> = {
      HOME: requireSandboxPath(process.env.HOME!), TMPDIR: requireSandboxPath(process.env.TMPDIR!),
      PATH: process.env.PATH!, BUN_INSTALL_CACHE_DIR: requireSandboxPath(process.env.BUN_INSTALL_CACHE_DIR!),
      FLEET_HOME: options.fleetHome, TELEGRAM_STATE_DIR: options.state,
      TELEBOTS_TEST: '1', TELEBOTS_SANDBOX_ROOT: process.env.TELEBOTS_SANDBOX_ROOT!,
      TELEBOTS_API_ROOT: options.apiRoot, SHIM_LOG: process.env.SHIM_LOG!, TZ: 'America/New_York',
    }
    if (options.accessMode) env.TELEGRAM_ACCESS_MODE = options.accessMode
    this.child = Bun.spawn([
      executable, import.meta.path, '--fake-owner', ...(options.channels === false ? [] : ['--channels', 'plugin:telegram@claude-plugins-official']), '--server', options.server,
    ], { cwd: root, env, stdin: 'pipe', stdout: 'pipe', stderr: 'pipe' })
    this.reading = this.readOutput()
    this.readingErrors = this.readErrors()
  }

  async initialize(): Promise<InitializeResult> {
    this.initialized = await this.request('initialize', {
      protocolVersion: '2024-11-05', capabilities: {}, clientInfo: { name: 'telebots-sandbox-client', version: '0.3.0' },
    }) as InitializeResult
    this.notify('notifications/initialized', {})
    return this.initialized
  }

  async listTools(): Promise<ListToolsResult> { return await this.request('tools/list', {}) as ListToolsResult }
  async callTool(name: string, args: Record<string, unknown>): Promise<CallToolResult> {
    return await this.request('tools/call', { name, arguments: args }) as CallToolResult
  }

  private notify(method: string, params: unknown): void {
    this.child.stdin.write(`${JSON.stringify({ jsonrpc: '2.0', method, params })}\n`)
  }

  async request(method: string, params: unknown): Promise<unknown> {
    const id = ++this.sequence
    return await new Promise((resolveValue, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id)
        reject(new Error(`MCP request ${method} timed out. ${this.stderr.slice(-1500)}`))
      }, 10_000)
      this.pending.set(id, { resolve: resolveValue, reject, timer })
      this.child.stdin.write(`${JSON.stringify({ jsonrpc: '2.0', id, method, params })}\n`)
    })
  }

  async close(): Promise<void> {
    this.child.stdin.end()
    const exited = await Promise.race([
      this.child.exited,
      Bun.sleep(8_000).then(() => { throw new Error('Fake owner did not exit after stdin EOF; no signal was sent.') }),
    ])
    await Promise.all([this.reading, this.readingErrors])
    if (exited !== 0) throw new Error(`Fake owner exited ${exited}: ${this.stderr.slice(-1500)}`)
  }

  private async readOutput(): Promise<void> {
    const reader = this.child.stdout.getReader()
    const decoder = new TextDecoder()
    let buffer = ''
    while (true) {
      const { done, value } = await reader.read()
      if (done) break
      buffer += decoder.decode(value, { stream: true })
      let newline: number
      while ((newline = buffer.indexOf('\n')) >= 0) {
        const line = buffer.slice(0, newline); buffer = buffer.slice(newline + 1)
        if (!line) continue
        const message = JSON.parse(line) as { id?: number; result?: unknown; error?: { message: string }; method?: string; params?: ChannelNotification['params'] }
        if (message.id != null) {
          const pending = this.pending.get(message.id)
          if (pending) {
            clearTimeout(pending.timer); this.pending.delete(message.id)
            if (message.error) pending.reject(new Error(message.error.message))
            else pending.resolve(message.result)
          }
        } else if (message.method) this.notifications.push(message as ChannelNotification)
      }
    }
    for (const pending of this.pending.values()) { clearTimeout(pending.timer); pending.reject(new Error('MCP transport ended.')) }
    this.pending.clear()
  }

  private async readErrors(): Promise<void> {
    for await (const chunk of this.child.stderr) this.stderr = (this.stderr + new TextDecoder().decode(chunk)).slice(-32_000)
  }
}

if (import.meta.main && process.argv.includes('--fake-owner')) {
  const index = process.argv.indexOf('--server')
  if (index < 0) throw new Error('Missing sandbox server.')
  const server = requireSandboxPath(resolve(process.argv[index + 1]!))
  const child = Bun.spawn([process.execPath, server], { cwd: dirname(server), env: process.env, stdin: 'pipe', stdout: 'pipe', stderr: 'pipe' })
  const output = (async () => { for await (const chunk of child.stdout) process.stdout.write(chunk) })()
  const errors = (async () => { for await (const chunk of child.stderr) process.stderr.write(chunk) })()
  for await (const chunk of process.stdin) child.stdin.write(chunk)
  child.stdin.end()
  process.exitCode = await child.exited
  await Promise.all([output, errors])
}
