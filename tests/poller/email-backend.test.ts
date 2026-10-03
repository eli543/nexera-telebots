import { test, expect } from 'bun:test'
import { spawnSync } from 'node:child_process'
import { join, resolve, sep } from 'node:path'

const sandbox = process.env.TELEBOTS_SANDBOX_ROOT
if (!sandbox || process.env.TELEGRAM_BOT_TOKEN || ![process.env.HOME, process.env.TMPDIR, process.env.FLEET_HOME, process.env.TELEGRAM_STATE_DIR]
  .every(value => value && resolve(value).startsWith(resolve(sandbox) + sep))) throw Error('Run through the scrubbed sandbox wrapper.')

test('email backend limits inbox fetches and returns complete bounded reads', () => {
  const script = join(import.meta.dir, 'email-backend-regression.py')
  const result = spawnSync('python3', ['-B', script], { encoding: 'utf8', env: process.env })
  expect(result.status, result.stderr || result.stdout).toBe(0)
})
