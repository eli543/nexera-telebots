import { test, expect } from 'bun:test'
import { readFileSync } from 'node:fs'
import { join, resolve, sep } from 'node:path'

const sandbox = process.env.TELEBOTS_SANDBOX_ROOT
if (!sandbox || process.env.TELEGRAM_BOT_TOKEN || ![process.env.HOME, process.env.TMPDIR, process.env.FLEET_HOME, process.env.TELEGRAM_STATE_DIR]
  .every(value => value && resolve(value).startsWith(resolve(sandbox) + sep))) throw Error('Run through the scrubbed sandbox wrapper.')
const repo = resolve(import.meta.dir, '../..')

test('Apache text and excerpt notice accompany the seam patch', () => {
  const license = readFileSync(join(repo, 'LICENSE-APACHE-2.0'), 'utf8')
  const notice = readFileSync(join(repo, 'NOTICE.md'), 'utf8')
  expect(license.length).toBeGreaterThan(11000)
  expect(license).toContain('Apache License')
  expect(license).toContain('Version 2.0, January 2004')
  expect(license).toContain('END OF TERMS AND CONDITIONS')
  expect(notice).toContain('LICENSE-APACHE-2.0')
  expect(notice).toContain('complete upstream server')
  expect(notice).toContain('context and removed lines')
})
