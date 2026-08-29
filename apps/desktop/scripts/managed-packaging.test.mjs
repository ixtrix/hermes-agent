import assert from 'node:assert/strict'
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { test } from 'vitest'

import { electronBundlePlan } from './bundle-electron-main.mjs'
import { managedBuilderConfig } from './run-electron-builder.mjs'
import { managedBuildDefines } from '../vite.config.ts'
import { inspectMacArchive } from './inspect-managed-package.mjs'

const here = path.dirname(fileURLToPath(import.meta.url))
const desktopRoot = path.resolve(here, '..')
const packageJson = JSON.parse(fs.readFileSync(path.join(desktopRoot, 'package.json'), 'utf8'))

test('managed builder identity stays separate while ordinary metadata is unchanged', () => {
  assert.equal(packageJson.build.appId, 'com.nousresearch.hermes')
  assert.equal(packageJson.build.productName, 'Hermes')
  assert.equal(packageJson.build.executableName, 'Hermes')
  assert.equal(packageJson.build.artifactName, 'Hermes-${version}-${os}-${arch}.${ext}')

  for (const [plane, label, id, scheme, executable] of [
    ['external', 'External', 'uk.co.scopefurnishing.hermes.external', 'scope-hermes-external', 'ScopeHermesExternal'],
    ['internal', 'Internal', 'uk.co.scopefurnishing.hermes.internal', 'scope-hermes-internal', 'ScopeHermesInternal']
  ]) {
    const managed = JSON.parse(fs.readFileSync(managedBuilderConfig(plane), 'utf8'))
    const productName = `Scope Hermes ${label}`
    const artifactBase = `Scope-Hermes-${label}`

    assert.equal(managed.appId, id)
    assert.equal(managed.productName, productName)
    assert.equal(managed.executableName, executable)
    assert.equal(managed.artifactName, `${artifactBase}-\${os}-\${arch}.\${ext}`)
    assert.deepEqual(managed.protocols, [{ name: `${productName} Protocol`, schemes: [scheme] }])
    assert.deepEqual(managed.extraMetadata.dependencies, {})
    assert.equal(managed.mac.artifactName, `${artifactBase}-macOS-arm64.\${ext}`)
    assert.equal(managed.mac.executableName, productName)
    assert.deepEqual(managed.mac.target, ['zip'])
    assert.equal(managed.mac.entitlements, 'electron/managed-entitlements.mac.plist')
    assert.equal(managed.mac.entitlementsInherit, 'electron/managed-entitlements.mac.inherit.plist')
    assert.equal(managed.win.artifactName, `${artifactBase}-Windows-x64.\${ext}`)
    assert.deepEqual(managed.win.target, ['nsis'])
    assert.deepEqual(Object.keys(managed.mac.extendInfo).sort(), [
      'CFBundleDisplayName',
      'CFBundleExecutable',
      'CFBundleName'
    ])
    assert.ok(managed.files.includes('!node_modules/node-pty/**'))
  }
})

test('built managed macOS archives contain the release bundle contract', () => {
  for (const plane of ['internal', 'external']) {
    const archive = process.env[`HERMES_MANAGED_${plane.toUpperCase()}_MAC_ARCHIVE`] ||
      path.join(desktopRoot, 'release', `Scope-Hermes-${plane[0].toUpperCase() + plane.slice(1)}-macOS-arm64.zip`)
    if (fs.existsSync(archive)) {
      inspectMacArchive(archive, plane)
    }
  }
})

test('managed and ordinary Electron bundles select isolated entrypoints and externals', () => {
  const managed = electronBundlePlan('external')
  assert.equal(path.basename(managed.mainEntry), 'managed-main.ts')
  assert.deepEqual(managed.external, ['electron', 'fs'])

  const ordinary = electronBundlePlan('')
  assert.equal(path.basename(ordinary.mainEntry), 'main.ts')
  assert.deepEqual(ordinary.external, ['electron', 'node-pty', 'get-windows', 'fs'])
})

test('renderer compile-time defines reject loopback authorities and select managed policy', () => {
  assert.equal(managedBuildDefines({}).__HERMES_DESKTOP_BUILD_PRODUCT__, 'null')
  const managed = managedBuildDefines({
    HERMES_DESKTOP_BUILD_PRODUCT: 'external',
    HERMES_DESKTOP_MANAGED_ORIGIN: 'https://hermes.example.test',
    HERMES_DESKTOP_MANAGED_OIDC_ISSUER: 'https://login.example.test',
    HERMES_DESKTOP_MANAGED_OIDC_AUDIENCE: 'staff',
    HERMES_DESKTOP_MANAGED_OIDC_REDIRECT_URI: 'https://app.example.test/callback'
  })
  assert.equal(managed.__HERMES_DESKTOP_BUILD_PRODUCT__, JSON.stringify('external'))
  assert.throws(
    () =>
      managedBuildDefines({
        HERMES_DESKTOP_BUILD_PRODUCT: 'external',
        HERMES_DESKTOP_MANAGED_ORIGIN: 'http://127.0.0.1:9119',
        HERMES_DESKTOP_MANAGED_OIDC_ISSUER: 'https://login.example.test',
        HERMES_DESKTOP_MANAGED_OIDC_AUDIENCE: 'staff',
        HERMES_DESKTOP_MANAGED_OIDC_REDIRECT_URI: 'https://app.example.test/callback'
      }),
    /loopback/
  )
})
