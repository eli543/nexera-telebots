import { afterEach, describe, expect, test } from 'bun:test'
import { createHash } from 'node:crypto'
import { copyFileSync, existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { join, resolve, sep } from 'node:path'

const root = process.env.TELEBOTS_SANDBOX_ROOT ?? ''
if (!root || process.env.TELEGRAM_BOT_TOKEN || ![process.env.HOME, process.env.TMPDIR, process.env.FLEET_HOME, process.env.TELEGRAM_STATE_DIR]
  .every(value => value && resolve(value).startsWith(resolve(root) + sep))) throw Error('Run through the scrubbed sandbox wrapper.')
const repo = resolve(import.meta.dir, '../..')
const stock = readFileSync(join(root, 'inputs/stock-server.ts'))
if (createHash('sha256').update(stock).digest('hex') !== 'd902d7195a527b579b889cfdbf251fc24fd224a68e319ac6b7c3b691515c3458') throw Error('Pinned stock fixture is required.')
const created: string[] = []
afterEach(() => { for (const path of created.splice(0)) rmSync(path, { recursive: true, force: true }) })

function fixture(slotKind: 'tampered' | 'newer' | 'stock' | 'absent') {
  const dir = mkdtempSync(join(root, 'uninstall-')); created.push(dir)
  const home = join(dir, 'home'), fleet = join(home, '.claude/telegram-fleet')
  const agents = join(home, 'Library/LaunchAgents')
  mkdirSync(fleet, { recursive: true }); mkdirSync(agents, { recursive: true })
  const plist = join(agents, 'com.claude-fleet.watchdog.plist')
  writeFileSync(plist, 'sandbox plist\n')
  const version = slotKind === 'newer' ? '0.0.8' : '0.0.7'
  const slot = join(home, '.claude/plugins/cache/claude-plugins-official/telegram', version)
  if (slotKind !== 'absent') {
    mkdirSync(join(slot, '.claude-plugin'), { recursive: true })
    writeFileSync(join(slot, '.claude-plugin/plugin.json'), JSON.stringify({ version }))
    if (slotKind === 'tampered') writeFileSync(join(slot, 'server.ts'), 'unknown local modification\n')
    else if (slotKind === 'stock') copyFileSync(join(root, 'inputs/stock-server.ts'), join(slot, 'server.ts'))
    else writeFileSync(join(slot, 'server.ts'), 'new upstream version\n')
  }
  const env: Record<string, string> = Object.fromEntries(Object.entries(process.env).filter((pair): pair is [string, string] => pair[1] !== undefined))
  Object.assign(env, { HOME: home, FLEET_HOME: fleet, TELEGRAM_STATE_DIR: join(home, '.claude/channels/test'), TMPDIR: join(dir, 'tmp') })
  mkdirSync(env.TMPDIR!, { recursive: true }); mkdirSync(env.TELEGRAM_STATE_DIR!, { recursive: true })
  const run = () => Bun.spawnSync(['/bin/bash', join(repo, 'uninstall.sh'), '--yes'], { env, stdout: 'pipe', stderr: 'pipe' })
  return { run, plist, fleet, slot }
}

describe('uninstall restoration preflight', () => {
  test('unknown modified server refuses before removing a plist or fleet files', () => {
    const f = fixture('tampered'), result = f.run()
    expect(result.exitCode).not.toBe(0)
    expect(existsSync(f.plist)).toBe(true)
    expect(existsSync(f.fleet)).toBe(true)
  })
  test.each(['newer', 'stock', 'absent'] as const)('%s plugin state removes Telebots without touching upstream', kind => {
    const f = fixture(kind)
    const before = kind === 'absent' ? null : readFileSync(join(f.slot, 'server.ts'))
    const result = f.run()
    expect(result.exitCode, result.stderr.toString()).toBe(0)
    expect(existsSync(f.plist)).toBe(false)
    expect(existsSync(f.fleet)).toBe(false)
    if (before) expect(readFileSync(join(f.slot, 'server.ts'))).toEqual(before)
  })
})
