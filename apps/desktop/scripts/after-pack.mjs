/**
 * electron-builder afterPack hook.
 *
 * Managed macOS bundles use the human-readable spaced app root required by the
 * release contract, while the inner executable remains a safe shell name.
 * The managed plist is also reduced to the explicitly allowed identity keys;
 * Electron's broad defaults must not turn into local or device authority.
 */

import fs from 'node:fs'
import path from 'node:path'
import { spawnSync } from 'node:child_process'

import { stampExeIdentity } from './set-exe-identity.mjs'

const MANAGED_PRODUCT_RE = /^Scope Hermes (Internal|External)$/
const FORBIDDEN_MANAGED_PLIST_KEYS = [
  'NSAppTransportSecurity',
  'NSAudioCaptureUsageDescription',
  'NSBluetoothAlwaysUsageDescription',
  'NSBluetoothPeripheralUsageDescription',
  'NSCameraUsageDescription',
  'NSMicrophoneUsageDescription'
]

function failPlistCommand(args, context) {
  const result = spawnSync('/usr/bin/plutil', args, { encoding: 'utf8' })
  if (result.status !== 0) {
    throw new Error(`managed macOS ${context} failed: ${result.stderr.trim() || `exit ${result.status}`}`)
  }
}

function sanitizeManagedMacBundle(context, productName) {
  const [, label] = productName.match(MANAGED_PRODUCT_RE)
  const executableName = `ScopeHermes${label}`
  const bundle = path.join(context.appOutDir, `${productName}.app`)
  const contents = path.join(bundle, 'Contents')
  const plist = path.join(contents, 'Info.plist')
  const sourceExecutable = path.join(contents, 'MacOS', productName)
  const safeExecutable = path.join(contents, 'MacOS', executableName)

  if (!fs.existsSync(bundle) || !fs.existsSync(plist)) {
    throw new Error(`managed macOS bundle is missing ${bundle} or Info.plist`)
  }
  failPlistCommand(['-lint', plist], 'Info.plist validation')
  if (fs.existsSync(sourceExecutable)) {
    fs.renameSync(sourceExecutable, safeExecutable)
  } else if (!fs.existsSync(safeExecutable)) {
    throw new Error(`managed macOS executable is missing: ${productName}`)
  }

  failPlistCommand(['-replace', 'CFBundleExecutable', '-string', executableName, plist], 'executable identity update')
  for (const key of FORBIDDEN_MANAGED_PLIST_KEYS) {
    const result = spawnSync('/usr/bin/plutil', ['-remove', key, plist], { encoding: 'utf8' })
    if (result.status !== 0 && !result.stderr.includes('Could not extract')) {
      throw new Error(`managed macOS plist key removal failed for ${key}: ${result.stderr.trim()}`)
    }
  }
  const plistText = fs.readFileSync(plist, 'utf8')
  for (const key of FORBIDDEN_MANAGED_PLIST_KEYS) {
    if (plistText.includes(`<key>${key}</key>`)) {
      throw new Error(`managed macOS Info.plist retains forbidden key ${key}`)
    }
  }
}

export default async function afterPack(context) {
  const productName = context.packager?.appInfo?.productFilename || ''
  if (context.electronPlatformName === 'darwin' && MANAGED_PRODUCT_RE.test(productName)) {
    sanitizeManagedMacBundle(context, productName)
    return
  }
  if (context.electronPlatformName !== 'win32') {
    return
  }

  const exe = path.join(context.appOutDir, `${productName || 'Hermes'}.exe`)
  const desktopRoot = path.resolve(import.meta.dirname, '..')
  try {
    await stampExeIdentity(exe, desktopRoot)
  } catch (err) {
    // Keep the existing best-effort cosmetic stamp behavior for ordinary and
    // managed Windows packages.
    console.warn(`[after-pack] exe identity stamp failed (${err.message}); executable keeps its current resources`)
  }
}
