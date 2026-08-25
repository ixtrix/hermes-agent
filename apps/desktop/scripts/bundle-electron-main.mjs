#!/usr/bin/env node
// Bundle the ordinary or managed Electron main/preload entry into dist/.

import { build } from 'esbuild'
import { resolve, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'
import { mkdirSync } from 'node:fs'

const here = dirname(fileURLToPath(import.meta.url))
const root = resolve(here, '..')

export function electronBundlePlan(plane = process.env.HERMES_DESKTOP_BUILD_PRODUCT || '') {
  const managed = plane === 'internal' || plane === 'external'
  return {
    external: managed ? ['electron', 'fs'] : ['electron', 'node-pty', 'fs'],
    mainEntry: resolve(root, managed ? 'electron/managed-main.ts' : 'electron/main.ts'),
    managed,
    plane
  }
}

const plane = process.env.HERMES_DESKTOP_BUILD_PRODUCT || ''
const { external, mainEntry, managed } = electronBundlePlan(plane)
const distDir = resolve(root, 'dist')
const mainOut = resolve(distDir, 'electron-main.mjs')
const preloadEntry = resolve(root, 'electron/preload.ts')
const preloadOut = resolve(distDir, 'electron-preload.js')
// Production bundles bake packaged=true so unpackaged `electron .` still
// behaves like a packaged build. Dev bundles leave the env alone.
const isDev = process.argv.includes('--dev')
const define = isDev
  ? {}
  : { 'process.env.HERMES_DESKTOP_IS_PACKAGED': JSON.stringify(true) }

function managedBuildDefines() {
  const fields = {
    origin: process.env.HERMES_DESKTOP_MANAGED_ORIGIN,
    issuer: process.env.HERMES_DESKTOP_MANAGED_OIDC_ISSUER,
    audience: process.env.HERMES_DESKTOP_MANAGED_OIDC_AUDIENCE,
    redirectUri: process.env.HERMES_DESKTOP_MANAGED_OIDC_REDIRECT_URI
  }

  if (!plane) {
    if (Object.keys(process.env).some(key => key.startsWith('HERMES_DESKTOP_MANAGED_'))) {
      throw new Error('Managed build inputs require HERMES_DESKTOP_BUILD_PRODUCT=internal|external.')
    }

    return Object.fromEntries([
      ['__HERMES_DESKTOP_BUILD_PRODUCT__', JSON.stringify(null)],
      ...Object.keys(fields).map(name => [`__HERMES_DESKTOP_MANAGED_${name.replace(/[A-Z]/g, letter => `_${letter}`).toUpperCase()}__`, JSON.stringify(null)])
    ])
  }

  if (!managed || Object.values(fields).some(value => !value)) {
    throw new Error('Managed builds require a valid plane and all origin/OIDC inputs.')
  }

  for (const [name, value] of Object.entries(fields)) {
    if (name === 'audience') {
      continue
    }

    const parsed = new URL(value)
    const hostname = parsed.hostname.toLowerCase()

    if (hostname === 'localhost' || hostname === '[::1]' || hostname.startsWith('127.')) {
      throw new Error(`Managed ${name} cannot use a loopback host.`)
    }

    if (parsed.protocol !== 'https:' || parsed.username || parsed.password || parsed.hash || parsed.search) {
      throw new Error(`Managed ${name} must be an HTTPS URL without credentials, query, or fragment.`)
    }

  }

  return {
    __HERMES_DESKTOP_BUILD_PRODUCT__: JSON.stringify(plane),
    __HERMES_DESKTOP_MANAGED_ORIGIN__: JSON.stringify(fields.origin),
    __HERMES_DESKTOP_MANAGED_OIDC_ISSUER__: JSON.stringify(fields.issuer),
    __HERMES_DESKTOP_MANAGED_OIDC_AUDIENCE__: JSON.stringify(fields.audience),
    __HERMES_DESKTOP_MANAGED_OIDC_REDIRECT_URI__: JSON.stringify(fields.redirectUri)
  }
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  mkdirSync(distDir, { recursive: true })
  const managedDefine = managedBuildDefines()

  await build({
    entryPoints: [mainEntry],
    bundle: true,
    platform: 'node',
    format: 'esm',
    target: 'node20',
    outfile: mainOut,
    external,
    banner: {
      js: "import { createRequire } from 'module'; const require = createRequire(import.meta.url);"
    },
    define: { ...define, ...managedDefine }
  })
  console.log(`bundled ${mainOut}${managed ? ` (${plane})` : ''}${isDev ? ' (dev)' : ''}`)

  await build({
    entryPoints: [preloadEntry],
    bundle: true,
    platform: 'node',
    format: 'cjs',
    target: 'node20',
    outfile: preloadOut,
    external,
    define: { ...define, ...managedDefine },
    logLevel: 'info'
  })
  console.log(`bundled ${preloadOut}${managed ? ` (${plane})` : ''}${isDev ? ' (dev)' : ''}`)
}
