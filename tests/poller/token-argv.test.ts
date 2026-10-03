import { describe, expect, test } from 'bun:test'
import { readFileSync, readdirSync } from 'node:fs'
import { join, resolve, sep } from 'node:path'

const root = resolve(import.meta.dir, '../..')
const sandbox = process.env.TELEBOTS_SANDBOX_ROOT
if (!sandbox || process.env.TELEGRAM_BOT_TOKEN || ![process.env.HOME, process.env.TMPDIR, process.env.FLEET_HOME, process.env.TELEGRAM_STATE_DIR]
  .every(value => value && resolve(value).startsWith(resolve(sandbox) + sep))) throw Error('Run through the scrubbed sandbox wrapper.')

describe('Telegram shell transport', () => {
  test('no curl argv contains a bot token URL', () => {
    const findings: string[] = []
    for (const directory of ['engine', 'daemon', 'plugin/hooks']) {
      for (const name of readdirSync(join(root, directory))) {
        const path = join(root, directory, name)
        const source = readFileSync(path, 'utf8')
        if (!source.startsWith('#!') || !/\b(?:ba)?sh\b/.test(source.split('\n', 1)[0]!)) continue
        for (const [index, line] of source.replace(/\\\n/g, '').split('\n').entries()) {
          if (/\b(?:curl|CURL)\b/.test(line) && /\/bot(?:\$\{?[A-Za-z_0-9]|[0-9]{8,10}:)/.test(line)) findings.push(`${directory}/${name}:${index + 1}`)
        }
      }
    }
    expect(findings).toEqual([])
  })
})
