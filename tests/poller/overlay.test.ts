import { afterEach, describe, expect, setDefaultTimeout, test } from 'bun:test'
import { createHash } from 'node:crypto'
import { copyFileSync, existsSync, lstatSync, mkdirSync, mkdtempSync, readFileSync, readdirSync, realpathSync, rmSync, statSync, symlinkSync, utimesSync, writeFileSync } from 'node:fs'
import { join, resolve, sep } from 'node:path'
import { runInNewContext } from 'node:vm'

// Never let the runner's timeout handler signal a sandbox process.
setDefaultTimeout(0)

const root = process.env.TELEBOTS_SANDBOX_ROOT
if (!root || process.env.TELEBOTS_TEST !== '1' || process.env.TELEGRAM_BOT_TOKEN !== undefined) {
  throw new Error('Overlay tests require the scrubbed sandbox wrapper and no token environment variable.')
}
const sandboxRoot = realpathSync(root)
for (const name of ['HOME', 'FLEET_HOME', 'TELEGRAM_STATE_DIR', 'TMPDIR']) {
  const value = process.env[name]
  if (!value || !(resolve(value) + sep).startsWith(sandboxRoot + sep)) throw new Error(`${name} must be inside the sandbox.`)
}
const stockPath = process.env.TELEBOTS_STOCK_SERVER ?? join(sandboxRoot, 'inputs', 'stock-server.ts')
if (!realpathSync(stockPath).startsWith(sandboxRoot + sep)) throw new Error('Stock fixture must be inside the sandbox.')
const stock = readFileSync(stockPath)
const stockSha = 'd902d7195a527b579b889cfdbf251fc24fd224a68e319ac6b7c3b691515c3458'
const sha = (value: string | Buffer) => createHash('sha256').update(value).digest('hex')
if (sha(stock) !== stockSha) throw new Error('Stock fixture does not match upstream 0.0.7.')
const source = resolve(import.meta.dir, '../../poller')
const modules = ['telebots.ts', 'email.ts', 'contracts.ts', 'ux.json'] as const
const files = [...modules, 'seam-0.0.7.patch', 'overlay-common.sh', 'install-overlay.sh', 'verify-overlay.sh', 'restore-stock.sh']
const patchedSha = /PATCHED_SHA='([a-f0-9]{64})'/.exec(readFileSync(join(source, 'install-overlay.sh'), 'utf8'))![1]!
const garbage: string[] = []
afterEach(() => { for (const path of garbage.splice(0)) rmSync(path, { recursive: true, force: true }) })

function fixture(version = '0.0.7', withSlot = true) {
  const dir = mkdtempSync(join(sandboxRoot, 'overlay-'))
  garbage.push(dir)
  const home = join(dir, 'home')
  const cache = join(home, '.claude/plugins/cache/claude-plugins-official/telegram')
  const slot = join(cache, version)
  const pkg = join(dir, 'package')
  for (const path of [home, pkg, join(dir, 'tmp'), join(dir, 'state'), join(dir, 'fleet')]) mkdirSync(path, { recursive: true })
  for (const name of files) copyFileSync(join(source, name), join(pkg, name))
  if (withSlot) {
    mkdirSync(join(slot, '.claude-plugin'), { recursive: true })
    writeFileSync(join(slot, '.claude-plugin/plugin.json'), JSON.stringify({ version }))
    writeFileSync(join(slot, 'server.ts'), stock)
  }
  const env: Record<string, string> = Object.fromEntries(Object.entries(process.env).filter((pair): pair is [string, string] => pair[1] !== undefined))
  Object.assign(env, { HOME: home, FLEET_HOME: join(dir, 'fleet'), TELEGRAM_STATE_DIR: join(dir, 'state'), TMPDIR: join(dir, 'tmp') })
  const run = (script: string, args: string[] = [], extra: Record<string, string> = {}) => {
    const result = Bun.spawnSync(['/bin/bash', join(pkg, script), ...args], { env: { ...env, ...extra }, stdout: 'pipe', stderr: 'pipe' })
    return { code: result.exitCode, out: result.stdout.toString(), err: result.stderr.toString() }
  }
  return { dir, home, cache, slot, pkg, env, run }
}
function tree(path: string): Record<string, string> {
  const result: Record<string, string> = {}
  if (!existsSync(path)) return result
  const visit = (current: string) => {
    for (const name of readdirSync(current).sort()) {
      const child = join(current, name)
      const stat = lstatSync(child)
      if (stat.isDirectory()) visit(child)
      else result[child.slice(path.length)] = `${stat.mode}:${stat.mtimeMs}:${stat.isSymbolicLink() ? 'symlink' : sha(readFileSync(child))}`
    }
  }
  visit(path)
  return result
}
function status(f: ReturnType<typeof fixture>, token: string, code: number) {
  expect(f.run('verify-overlay.sh', ['--status'])).toEqual({ code, out: token + '\n', err: '' })
}
function install(f: ReturnType<typeof fixture>) {
  const result = f.run('install-overlay.sh')
  expect(result.code, result.err).toBe(0)
  expect(sha(readFileSync(join(f.slot, 'server.ts')))).toBe(patchedSha)
}

describe('hash-pinned overlay lifecycle', () => {
  test('applies stock, deploys all imports, verifies, and is a byte-and-mtime no-op twice', () => {
    const f = fixture()
    status(f, 'STOCK', 10)
    install(f)
    status(f, 'OK', 0)
    for (const name of modules) expect(readFileSync(join(f.slot, name))).toEqual(readFileSync(join(source, name)))
    expect(sha(readFileSync(join(f.slot, 'server.ts.stock-backup')))).toBe(stockSha)
    const snapshot = tree(f.slot)
    install(f)
    install(f)
    expect(tree(f.slot)).toEqual(snapshot)
    for (const script of ['verify-overlay.sh', 'restore-stock.sh']) expect(readFileSync(join(source, script), 'utf8')).toContain(`PATCHED_SHA='${patchedSha}'`)
  })

  test('reports an absent cache and a missing newest server precisely', () => {
    const f = fixture('0.0.7', false)
    status(f, 'NOT_INSTALLED', 14)
    expect(f.run('install-overlay.sh').code).toBe(14)
    mkdirSync(join(f.slot, '.claude-plugin'), { recursive: true })
    writeFileSync(join(f.slot, '.claude-plugin/plugin.json'), '{"version":"0.0.7"}')
    status(f, 'NOT_INSTALLED', 14)
  })

  test.each(['0.0.6', '0.0.8'])('refuses version %s without changing any slot', version => {
    const f = fixture(version)
    const before = tree(f.cache)
    status(f, `VERSION_MISMATCH:${version}`, 13)
    expect(f.run('install-overlay.sh').code).toBe(13)
    expect(f.run('restore-stock.sh').code).toBe(13)
    expect(tree(f.cache)).toEqual(before)
  })

  test('newest version wins even if the old server has a newer timestamp', () => {
    const f = fixture()
    const newer = join(f.cache, '0.0.8')
    mkdirSync(join(newer, '.claude-plugin'), { recursive: true })
    writeFileSync(join(newer, '.claude-plugin/plugin.json'), '{"version":"0.0.8"}')
    const future = new Date(Date.now() + 60_000)
    utimesSync(join(f.slot, 'server.ts'), future, future)
    status(f, 'VERSION_MISMATCH:0.0.8', 13)
    expect(f.run('install-overlay.sh').code).toBe(13)
    expect(sha(readFileSync(join(f.slot, 'server.ts')))).toBe(stockSha)
  })

  test('invalid plugin metadata prints one safe unknown-version token', () => {
    const f = fixture()
    writeFileSync(join(f.slot, '.claude-plugin/plugin.json'), '{"version":"bad\\nvalue"}')
    status(f, 'VERSION_MISMATCH:unknown', 13)
  })

  test('tampered server is refused on install and restore', () => {
    const f = fixture()
    writeFileSync(join(f.slot, 'server.ts'), Buffer.concat([stock, Buffer.from('\n// local change\n')]))
    const before = tree(f.slot)
    status(f, 'TAMPERED', 12)
    expect(f.run('install-overlay.sh').code).toBe(12)
    expect(f.run('restore-stock.sh').code).toBe(12)
    expect(tree(f.slot)).toEqual(before)
  })

  test.each([...modules])('missing %s is detected and repaired while keeping the stock backup', name => {
    const f = fixture()
    install(f)
    const backup = statSync(join(f.slot, 'server.ts.stock-backup')).mtimeMs
    rmSync(join(f.slot, name))
    status(f, 'MISSING_MODULE', 11)
    install(f)
    status(f, 'OK', 0)
    expect(statSync(join(f.slot, 'server.ts.stock-backup')).mtimeMs).toBe(backup)
  })

  test('module and manifest tampering are detected', () => {
    const f = fixture()
    install(f)
    writeFileSync(join(f.slot, 'email.ts'), '// modified\n')
    status(f, 'TAMPERED', 12)
    install(f)
    rmSync(join(f.slot, '.telebots-overlay.json'))
    status(f, 'TAMPERED', 12)
    install(f)
    status(f, 'OK', 0)
  })

  test('a poisoned backup refuses both mutations without corruption', () => {
    const f = fixture()
    install(f)
    writeFileSync(join(f.slot, 'server.ts.stock-backup'), 'not stock\n')
    const before = tree(f.slot)
    status(f, 'TAMPERED', 12)
    expect(f.run('install-overlay.sh').code).toBe(22)
    expect(f.run('restore-stock.sh').code).toBe(22)
    expect(tree(f.slot)).toEqual(before)
  })

  test('a patched file without its verified backup cannot be installed again', () => {
    const f = fixture()
    install(f)
    rmSync(join(f.slot, 'server.ts.stock-backup'))
    const before = tree(f.slot)
    expect(f.run('install-overlay.sh').code).toBe(22)
    expect(f.run('restore-stock.sh').code).toBe(22)
    expect(tree(f.slot)).toEqual(before)
  })

  test('incomplete package, bad patch, and wrong patched pin leave stock untouched', () => {
    const f = fixture()
    const before = tree(f.slot)
    rmSync(join(f.pkg, 'email.ts'))
    expect(f.run('install-overlay.sh').code).toBe(21)
    expect(tree(f.slot)).toEqual(before)
    copyFileSync(join(source, 'email.ts'), join(f.pkg, 'email.ts'))
    writeFileSync(join(f.pkg, 'seam-0.0.7.patch'), 'invalid patch\n')
    expect(f.run('install-overlay.sh').code).toBe(23)
    expect(tree(f.slot)).toEqual(before)
    copyFileSync(join(source, 'seam-0.0.7.patch'), join(f.pkg, 'seam-0.0.7.patch'))
    const script = join(f.pkg, 'install-overlay.sh')
    writeFileSync(script, readFileSync(script, 'utf8').replace(patchedSha, '0'.repeat(64)))
    expect(f.run('install-overlay.sh').code).toBe(23)
    expect(tree(f.slot)).toEqual(before)
  })

  test('symlink destinations and an existing mutation lock are refused', () => {
    const f = fixture()
    const sentinel = join(f.dir, 'sentinel.ts')
    writeFileSync(sentinel, 'preserve\n')
    symlinkSync(sentinel, join(f.slot, 'telebots.ts'))
    const before = tree(f.slot)
    expect(f.run('install-overlay.sh').code).toBe(12)
    expect(readFileSync(sentinel, 'utf8')).toBe('preserve\n')
    expect(tree(f.slot)).toEqual(before)
    rmSync(join(f.slot, 'telebots.ts'))
    mkdirSync(join(f.slot, '.telebots-overlay.lock'))
    expect(f.run('install-overlay.sh').code).toBe(24)
    expect(sha(readFileSync(join(f.slot, 'server.ts')))).toBe(stockSha)
  })

  test('an interrupted multi-file commit rolls back every applied file', () => {
    const f = fixture()
    const inject = join(f.dir, 'inject')
    mkdirSync(inject)
    writeFileSync(join(inject, 'sitecustomize.py'), `import os\noriginal = os.replace\ncount = 0\ndef fail_once(src, dst):\n    global count\n    if '/next-' in str(src):\n        count += 1\n        if count == 3:\n            raise OSError('injected write failure')\n    return original(src, dst)\nos.replace = fail_once\n`)
    const before = tree(f.slot)
    const result = f.run('install-overlay.sh', [], { PYTHONPATH: inject })
    expect(result.code, result.out + result.err).toBe(25)
    expect(result.err).toContain('previous bytes were restored')
    expect(tree(f.slot)).toEqual(before)
    install(f)
    writeFileSync(join(f.slot, 'email.ts'), '// preserve this prior module change\n')
    const beforeRepair = tree(f.slot)
    expect(f.run('install-overlay.sh', [], { PYTHONPATH: inject }).code).toBe(25)
    expect(tree(f.slot)).toEqual(beforeRepair)
  })

  test('restore verifies stock, removes owned support files, preserves changes, and repeats safely', () => {
    const f = fixture()
    install(f)
    writeFileSync(join(f.slot, 'email.ts'), '// local edit retained\n')
    expect(f.run('restore-stock.sh').code).toBe(0)
    status(f, 'STOCK', 10)
    expect(sha(readFileSync(join(f.slot, 'server.ts')))).toBe(stockSha)
    expect(readFileSync(join(f.slot, 'email.ts'), 'utf8')).toBe('// local edit retained\n')
    expect(existsSync(join(f.slot, 'telebots.ts'))).toBeFalse()
    expect(existsSync(join(f.slot, '.telebots-overlay.json'))).toBeFalse()
    expect(f.run('restore-stock.sh').code).toBe(0)
    expect(sha(readFileSync(join(f.slot, 'server.ts.stock-backup')))).toBe(stockSha)
  })

  test('unsupported flags cannot bypass version or hash gates', () => {
    const f = fixture()
    for (const script of ['install-overlay.sh', 'verify-overlay.sh', 'restore-stock.sh']) {
      expect(f.run(script, ['--force']).code).toBe(20)
      expect(f.run(script, ['--help', '--force']).code).toBe(20)
    }
    expect(sha(readFileSync(join(f.slot, 'server.ts')))).toBe(stockSha)
  })

  test('seam remains stock-based and its MCP instruction text stays under 2000 characters', () => {
    const f = fixture()
    install(f)
    const patched = readFileSync(join(f.slot, 'server.ts'), 'utf8')
    const literal = /instructions:\s*(\[[\s\S]*?\]\.join\('\\n'\))/.exec(patched)![1]!
    const instructions: unknown = runInNewContext(literal, {}, { timeout: 1000 })
    expect(typeof instructions).toBe('string')
    expect((instructions as string).length).toBeLessThanOrEqual(2000)
    expect(instructions).toContain('transcript="ok"')
    expect(patched.indexOf('tb?.registerHandlers()')).toBeLessThan(patched.indexOf("bot.on('callback_query:data'"))
    expect(patched.indexOf('tb?.registerHandlers()')).toBeLessThan(patched.indexOf('tb?.afterPidWrite()'))
    expect(patched.indexOf('tb?.intercept(ctx, text, chat_id)')).toBeLessThan(patched.indexOf('const permMatch ='))
    expect(existsSync(join(source, 'server.ts'))).toBeFalse()
    expect(readFileSync(join(source, 'seam-0.0.7.patch'), 'utf8')).not.toMatch(/\/Users\//)
  })
})
