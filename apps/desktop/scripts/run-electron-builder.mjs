// Resolve electronDist at runtime (#38673, #47917): electron-builder 26.8.x can
// re-unpack a broken Electron.app; reusing the installed dist dodges that.
// npm workspace hoisting is non-deterministic — require.resolve finds electron
// wherever it landed. Dist present → -c.electronDist=<abs>/dist; absent → let
// electron-builder fetch via @electron/get (electronVersion + ELECTRON_MIRROR).

import fs from 'node:fs'
import path from 'node:path'
import { spawnSync } from 'node:child_process'
import { createRequire } from 'node:module'
import { fileURLToPath } from 'node:url'

const require = createRequire(import.meta.url)
const desktopRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')

function electronDistDir() {
  try {
    return path.join(path.dirname(require.resolve('electron/package.json')), 'dist')
  } catch {
    return null
  }
}

function distBinary(dist) {
  if (process.platform === 'darwin') {
    return path.join(dist, 'Electron.app', 'Contents', 'MacOS', 'Electron')
  }
  if (process.platform === 'win32') {
    return path.join(dist, 'electron.exe')
  }
  return path.join(dist, 'electron')
}

function electronBuilderCli() {
  const pkgJson = require.resolve('electron-builder/package.json')
  const bin = require(pkgJson).bin
  const rel = typeof bin === 'string' ? bin : bin['electron-builder']
  return path.join(path.dirname(pkgJson), rel)
}

export function managedBuilderConfig(plane) {
  const packageJson = JSON.parse(fs.readFileSync(path.join(desktopRoot, 'package.json'), 'utf8'))
  const build = structuredClone(packageJson.build)
  const label = plane[0].toUpperCase() + plane.slice(1)
  const productName = `Scope Hermes ${label}`
  const executableName = `ScopeHermes${label}`
  const artifactBase = `Scope-Hermes-${label}`
  const managedDependencies = {}
  build.appId = `uk.co.scopefurnishing.hermes.${plane}`
  build.productName = productName
  build.executableName = executableName
  build.artifactName = `${artifactBase}-\${os}-\${arch}.\${ext}`
  build.protocols = [{ name: `${productName} Protocol`, schemes: [`scope-hermes-${plane}`] }]
  build.extraMetadata = { ...build.extraMetadata, dependencies: managedDependencies }
  build.files = [...(build.files || []), '!node_modules/node-pty/**', '!dist/node_modules/node-pty/**']
  build.mac = {
    ...build.mac,
    artifactName: `${artifactBase}-macOS-arm64.\${ext}`,
    executableName: productName,
    target: ['zip'],
    entitlements: 'electron/managed-entitlements.mac.plist',
    entitlementsInherit: 'electron/managed-entitlements.mac.inherit.plist',
    extendInfo: {
      CFBundleDisplayName: productName,
      CFBundleExecutable: executableName,
      CFBundleName: productName
    }
  }
  build.dmg = { ...build.dmg, title: `Install ${productName}` }
  build.win = {
    ...build.win,
    artifactName: `${artifactBase}-Windows-x64.\${ext}`,
    legalTrademarks: productName,
    target: ['nsis']
  }
  build.nsis = {
    ...build.nsis,
    shortcutName: productName,
    uninstallDisplayName: productName
  }

  const configPath = path.join(desktopRoot, 'build', `electron-builder-${plane}.json`)
  fs.mkdirSync(path.dirname(configPath), { recursive: true })
  fs.writeFileSync(configPath, JSON.stringify(build))

  return configPath
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const cliArgs = process.argv.slice(2)
  const crossTarget =
    (cliArgs.includes('--mac') && process.platform !== 'darwin') ||
    (cliArgs.includes('--win') && process.platform !== 'win32') ||
    (cliArgs.includes('--linux') && process.platform !== 'linux')
  const dist = electronDistDir()
  const args = []
  if (!crossTarget && dist && fs.existsSync(distBinary(dist))) {
    args.push(`-c.electronDist=${dist}`)
  } else {
    console.warn(
      '[run-electron-builder] local electron dist is unavailable for this target; ' +
        'electron-builder will fetch via @electron/get (electronVersion + ELECTRON_MIRROR).'
    )
  }

  const plane = process.env.HERMES_DESKTOP_BUILD_PRODUCT
  if (plane === 'internal' || plane === 'external') {
    args.push('--config', managedBuilderConfig(plane))
  }
  args.push(...cliArgs)

  const result = spawnSync(process.execPath, [electronBuilderCli(), ...args], {
    stdio: 'inherit'
  })
  if (result.error) {
    console.error(`[run-electron-builder] spawn failed: ${result.error.message}`)
    process.exit(1)
  }
  process.exit(result.status == null ? 1 : result.status)
}
