import assert from 'node:assert/strict'
import fs from 'node:fs'
import path from 'node:path'
import { spawnSync } from 'node:child_process'
import { fileURLToPath } from 'node:url'

const PRODUCTS = {
  internal: {
    bundle: 'Scope Hermes Internal.app',
    executable: 'ScopeHermesInternal',
    id: 'uk.co.scopefurnishing.hermes.internal',
    name: 'Scope Hermes Internal',
    protocol: 'scope-hermes-internal'
  },
  external: {
    bundle: 'Scope Hermes External.app',
    executable: 'ScopeHermesExternal',
    id: 'uk.co.scopefurnishing.hermes.external',
    name: 'Scope Hermes External',
    protocol: 'scope-hermes-external'
  }
}

const FORBIDDEN_PLIST_KEYS = [
  'NSAllowsArbitraryLoads',
  'NSAllowsLocalNetworking',
  'NSExceptionDomains',
  'NSAudioCaptureUsageDescription',
  'NSBluetoothAlwaysUsageDescription',
  'NSBluetoothPeripheralUsageDescription',
  'NSCameraUsageDescription',
  'NSMicrophoneUsageDescription'
]

function plistString(plist, key) {
  const escaped = key.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
  const match = plist.match(new RegExp(`<key>${escaped}</key>\\s*<string>([^<]*)</string>`))
  return match?.[1] ?? null
}

function inspectMacArchive(archivePath, plane) {
  const product = PRODUCTS[plane]
  assert(product, `unsupported managed plane: ${plane}`)
  assert(fs.statSync(archivePath).isFile(), `missing archive: ${archivePath}`)
  const listing = spawnSync('/usr/bin/unzip', ['-Z1', archivePath], { encoding: 'utf8' })
  assert.equal(listing.status, 0, listing.stderr)
  const roots = [...new Set(listing.stdout.split(/\r?\n/).filter(Boolean).map(entry => entry.split('/')[0]).filter(root => root.endsWith('.app')))]
  assert.deepEqual(roots, [product.bundle], 'archive must contain exactly one expected app root')

  const plistResult = spawnSync('/usr/bin/unzip', ['-p', archivePath, `${product.bundle}/Contents/Info.plist`], { encoding: 'utf8' })
  assert.equal(plistResult.status, 0, plistResult.stderr)
  const plist = plistResult.stdout
  assert.equal(plistString(plist, 'CFBundleIdentifier'), product.id)
  assert.equal(plistString(plist, 'CFBundleDisplayName'), product.name)
  assert.equal(plistString(plist, 'CFBundleName'), product.name)
  assert.equal(plistString(plist, 'CFBundleExecutable'), product.executable)
  for (const key of FORBIDDEN_PLIST_KEYS) {
    assert.equal(plist.includes(`<key>${key}</key>`), false, `forbidden plist key remains: ${key}`)
  }
  const protocols = [...plist.matchAll(/<key>CFBundleURLSchemes<\/key>\s*<array>\s*<string>([^<]*)<\/string>\s*<\/array>/g)].map(match => match[1])
  assert.deepEqual(protocols, [product.protocol])
  assert.ok(listing.stdout.split(/\r?\n/).includes(`${product.bundle}/Contents/MacOS/${product.executable}`))
  assert.equal(listing.stdout.split(/\r?\n/).includes(`${product.bundle}/Contents/MacOS/${product.name}`), false)
  return true
}

export { inspectMacArchive }

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const [archive, plane] = process.argv.slice(2)
  if (!archive || !plane) {
    throw new Error('usage: node inspect-managed-package.mjs ARCHIVE.zip internal|external')
  }
  inspectMacArchive(path.resolve(archive), plane)
  console.log(`verified managed ${plane} macOS archive contract`)
}
