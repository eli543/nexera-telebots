import { describe, expect, test } from 'bun:test'
import { readFileSync, readdirSync, statSync } from 'node:fs'
import { join, relative, resolve, sep } from 'node:path'

const root = resolve(import.meta.dir, '../..')
const sandbox = process.env.TELEBOTS_SANDBOX_ROOT
if (!sandbox || process.env.TELEGRAM_BOT_TOKEN || ![process.env.HOME, process.env.TMPDIR, process.env.FLEET_HOME, process.env.TELEGRAM_STATE_DIR]
  .every(value => value && resolve(value).startsWith(resolve(sandbox) + sep))) throw Error('Run through the scrubbed sandbox wrapper.')

const credentialArg = /(?:\s|^)(?:-H|--header)(?:\s+|=)[^\n]*(?:authorization|bearer|api[-_]?key|token)|\/bot(?:\$\{?[A-Za-z_][\w]*|[0-9]{8,10}:)|\$\{?[A-Za-z_]*(?:TOKEN|TOK|API_KEY|OAUTH|SECRET)\b|[0-9]{8,10}:[A-Za-z0-9_-]{35}/i

function unsafeCurlArgv(source: string): string[] {
  const logical = source.replace(/\\\r?\n\s*/g, ' ')
  const found: string[] = []
  for (const [index, line] of logical.split('\n').entries()) {
    // A piped curl --config - receives its secret on stdin. Inspect the curl
    // argument segment, not the preceding printf that builds the config.
    const match = /(?:^|[\s|(&])(?:["']?\/?[\w/-]*curl["']?|["']?\$CURL["']?)(?=\s|,)/i.exec(line)
    if (!match) continue
    const argv = line.slice(match.index + match[0].length)
    if (credentialArg.test(argv)) found.push(`${index + 1}: ${argv.trim().slice(0, 100)}`)
  }
  return found
}

function files(dir: string): string[] {
  return readdirSync(dir).flatMap(name => {
    const path = join(dir, name)
    return statSync(path).isDirectory() ? files(path) : [path]
  })
}

describe('curl credential transport', () => {
  test('scanner catches headers, bearer tokens, API keys and token URLs outside Telegram', () => {
    expect(unsafeCurlArgv('curl -H "Authorization: Bearer $OAUTH_TOK" https://example.test')).toHaveLength(1)
    expect(unsafeCurlArgv('curl --header "x-api-key: $API_KEY" https://example.test')).toHaveLength(1)
    expect(unsafeCurlArgv('curl "https://example.test/bot${TOKEN}/sendMessage"')).toHaveLength(1)
    expect(unsafeCurlArgv('printf "header = \\"Authorization: Bearer %s\\"" "$TOKEN" | curl --config - https://example.test')).toEqual([])
  })

  test('no curl argv in engines, daemon or hooks carries credentials', () => {
    const findings: string[] = []
    for (const directory of ['engine', 'daemon', 'plugin/hooks']) {
      for (const path of files(join(root, directory))) {
        const source = readFileSync(path, 'utf8')
        for (const finding of unsafeCurlArgv(source)) findings.push(`${relative(root, path)}:${finding}`)
      }
    }
    expect(findings).toEqual([])
  })
})
