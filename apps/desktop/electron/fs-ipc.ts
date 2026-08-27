// IPC surface for local filesystem operations the renderer's project/file
// surfaces use: directory reads, reveal/open in the OS file manager, plugin
// roots + git installs, rename/write/trash. Extracted from main.ts; path
// hardening, HERMES_HOME resolution, and the git binary stay injected.
import { createHash, randomUUID } from 'node:crypto'
import fs from 'node:fs'
import path from 'node:path'

import { app, ipcMain, shell } from 'electron'

import { installDesktopPluginFromGit, probePluginRepo } from './desktop-plugin-install'
import { readDirForIpc } from './fs-read-dir'
import { gitRootForIpc } from './git-root'
import { managedProductPlane, type ManagedProductPlane } from './managed-product'

export interface FsIpcDeps {
  hermesHome: string
  readActiveDesktopProfile: () => null | string
  expandUserPath: (value: string) => string
  resolveRequestedPathForIpc: (value: string, options: { purpose: string }) => string
  directoryExists: (value: string) => boolean
  resolveGitBinary: () => string
}

export type WorkstationFolderName = 'desktop' | 'documents' | 'downloads'

export interface WorkstationSourceReceipt {
  dev: number
  ino: number
  mtimeMs: number
  path: string
  sha256: string
  size: number
}

interface WorkstationFolderRoots {
  desktop: string
  documents: string
  downloads: string
}

interface WorkstationFoldersHandlerDeps {
  plane: ManagedProductPlane | null
  roots: WorkstationFolderRoots
  trashItem: (targetPath: string) => Promise<void>
}

const WORKSTATION_FILE_MAX_BYTES = 10_000_000
const WORKSTATION_LIST_MAX_ENTRIES = 200

function isInsideRoot(candidate: string, root: string): boolean {
  const relative = path.relative(root, candidate)

  return relative === '' || (!relative.startsWith(`..${path.sep}`) && relative !== '..' && !path.isAbsolute(relative))
}

function sameReceipt(left: WorkstationSourceReceipt, right: WorkstationSourceReceipt): boolean {
  return (
    left.path === right.path &&
    left.dev === right.dev &&
    left.ino === right.ino &&
    left.size === right.size &&
    left.mtimeMs === right.mtimeMs &&
    left.sha256 === right.sha256
  )
}

function decodeBoundedBase64(value: unknown): Buffer {
  if (typeof value !== 'string' || value.length > Math.ceil(WORKSTATION_FILE_MAX_BYTES / 3) * 4 + 4) {
    throw new Error('Invalid staged content')
  }
  if (value !== '' && (!/^[A-Za-z0-9+/]+={0,2}$/.test(value) || value.length % 4 !== 0)) {
    throw new Error('Invalid staged content')
  }
  const content = Buffer.from(value, 'base64')

  if (content.length > WORKSTATION_FILE_MAX_BYTES || content.toString('base64') !== value) {
    throw new Error('Invalid staged content')
  }

  return content
}

export function createWorkstationFoldersHandler({
  plane,
  roots,
  trashItem
}: WorkstationFoldersHandlerDeps) {
  let canonicalRootsPromise: Promise<WorkstationFolderRoots> | null = null

  const canonicalRoots = () => {
    canonicalRootsPromise ??= Promise.all([
      fs.promises.realpath(roots.desktop),
      fs.promises.realpath(roots.documents),
      fs.promises.realpath(roots.downloads)
    ]).then(([desktop, documents, downloads]) => ({ desktop, documents, downloads }))

    return canonicalRootsPromise
  }

  const resolveApprovedPath = async (rawPath: unknown) => {
    const requested = typeof rawPath === 'string' ? rawPath.trim() : ''

    if (!requested || !path.isAbsolute(requested)) {
      throw new Error('Path is outside approved workstation folders')
    }
    const canonical = await fs.promises.realpath(requested)
    const approvedRoots = Object.values(await canonicalRoots())

    if (!approvedRoots.some(root => isInsideRoot(canonical, root))) {
      throw new Error('Path is outside approved workstation folders')
    }

    return canonical
  }

  const sourceReceipt = async (sourcePath: string): Promise<{ content: Buffer; source: WorkstationSourceReceipt }> => {
    const before = await fs.promises.stat(sourcePath)

    if (!before.isFile()) {
      throw new Error('Only regular files can be staged')
    }
    if (before.size > WORKSTATION_FILE_MAX_BYTES) {
      throw new Error('Workstation files are limited to 10 MB')
    }
    const content = await fs.promises.readFile(sourcePath)
    const after = await fs.promises.stat(sourcePath)

    if (before.size !== after.size || before.mtimeMs !== after.mtimeMs) {
      throw new Error('Source changed while it was staged')
    }

    return {
      content,
      source: {
        dev: after.dev,
        ino: after.ino,
        mtimeMs: after.mtimeMs,
        path: sourcePath,
        sha256: createHash('sha256').update(content).digest('hex'),
        size: after.size
      }
    }
  }

  const resolveApprovedNewPath = async (rawPath: unknown) => {
    const requested = typeof rawPath === 'string' ? rawPath.trim() : ''

    if (!requested || !path.isAbsolute(requested) || path.basename(requested) !== path.basename(path.normalize(requested))) {
      throw new Error('Path is outside approved workstation folders')
    }
    const parent = await fs.promises.realpath(path.dirname(requested))
    const approvedRoots = Object.values(await canonicalRoots())

    if (!approvedRoots.some(root => isInsideRoot(parent, root))) {
      throw new Error('Path is outside approved workstation folders')
    }

    return path.join(parent, path.basename(requested))
  }
  return async (op: unknown, payload: unknown) => {
    if (plane !== 'internal') {
      throw new Error('Workstation folders are available only in Scope Hermes Internal')
    }

    if (op === 'roots') {
      return { ok: true, roots: await canonicalRoots() }
    }

    if (op === 'list') {
      const requestedPath = payload && typeof payload === 'object' && 'path' in payload ? payload.path : undefined
      const directory = await resolveApprovedPath(requestedPath)
      const stat = await fs.promises.stat(directory)

      if (!stat.isDirectory()) {
        throw new Error('Workstation list path is not a directory')
      }
      const children = await fs.promises.readdir(directory, { withFileTypes: true })
      const entries: Array<{ isDirectory: boolean; mtimeMs: number; name: string; path: string; size: number }> = []

      for (const child of children.slice(0, WORKSTATION_LIST_MAX_ENTRIES)) {
        const childPath = path.join(directory, child.name)

        try {
          const canonical = await resolveApprovedPath(childPath)
          const childStat = await fs.promises.stat(canonical)

          entries.push({
            isDirectory: childStat.isDirectory(),
            mtimeMs: childStat.mtimeMs,
            name: child.name,
            path: canonical,
            size: childStat.size
          })
        } catch {
          // A symlink that leaves the approved roots is not an item the renderer
          // can act on, so omit it rather than leaking its target.
        }
      }
      entries.sort((left, right) => left.name.localeCompare(right.name))

      return { entries, ok: true }
    }

    if (op === 'stage') {
      const requestedPath = payload && typeof payload === 'object' && 'path' in payload ? payload.path : undefined
      const sourcePath = await resolveApprovedPath(requestedPath)
      const staged = await sourceReceipt(sourcePath)

      return { contentBase64: staged.content.toString('base64'), ok: true, source: staged.source }
    }

    if (op === 'save') {
      const expectedValue =
        payload && typeof payload === 'object' && 'source' in payload ? payload.source : undefined

      if (
        !expectedValue ||
        typeof expectedValue !== 'object' ||
        !('path' in expectedValue) ||
        !('dev' in expectedValue) ||
        !('ino' in expectedValue) ||
        !('size' in expectedValue) ||
        !('mtimeMs' in expectedValue) ||
        !('sha256' in expectedValue) ||
        typeof expectedValue.path !== 'string' ||
        typeof expectedValue.dev !== 'number' ||
        typeof expectedValue.ino !== 'number' ||
        typeof expectedValue.size !== 'number' ||
        typeof expectedValue.mtimeMs !== 'number' ||
        typeof expectedValue.sha256 !== 'string'
      ) {
        throw new Error('Invalid staged source metadata')
      }
      const expected: WorkstationSourceReceipt = {
        dev: expectedValue.dev,
        ino: expectedValue.ino,
        mtimeMs: expectedValue.mtimeMs,
        path: expectedValue.path,
        sha256: expectedValue.sha256,
        size: expectedValue.size
      }
      const sourcePath = await resolveApprovedPath(expected.path)
      const current = await sourceReceipt(sourcePath)

      if (!sameReceipt(expected, current.source)) {
        return { error: 'source-conflict', ok: false }
      }
      const encodedContent =
        payload && typeof payload === 'object' && 'contentBase64' in payload ? payload.contentBase64 : undefined
      const content = decodeBoundedBase64(encodedContent)
      const tempPath = path.join(path.dirname(sourcePath), `.${path.basename(sourcePath)}.hermes-${randomUUID()}.tmp`)

      try {
        await fs.promises.writeFile(tempPath, content, { flag: 'wx', mode: 0o600 })
        const immediatelyBeforeSave = await sourceReceipt(sourcePath)

        if (!sameReceipt(current.source, immediatelyBeforeSave.source)) {
          return { error: 'source-conflict', ok: false }
        }
        await fs.promises.rename(tempPath, sourcePath)
      } finally {
        await fs.promises.rm(tempPath, { force: true }).catch(() => undefined)
      }
      const saved = await sourceReceipt(sourcePath)

      return { ok: true, source: saved.source }
    }

    if (op === 'create') {
      const requestedPath = payload && typeof payload === 'object' && 'path' in payload ? payload.path : undefined
      const targetPath = await resolveApprovedNewPath(requestedPath)
      const encodedContent =
        payload && typeof payload === 'object' && 'contentBase64' in payload ? payload.contentBase64 : undefined
      const content = decodeBoundedBase64(encodedContent)

      try {
        await fs.promises.writeFile(targetPath, content, { flag: 'wx', mode: 0o600 })
      } catch (error) {
        if ((error as NodeJS.ErrnoException).code === 'EEXIST') {
          throw new Error('Destination already exists')
        }

        throw error
      }
      const created = await sourceReceipt(targetPath)

      return { ok: true, source: created.source }
    }

    if (op === 'move') {
      const value = payload && typeof payload === 'object' ? payload : null
      const sourcePath = await resolveApprovedPath(value && 'sourcePath' in value ? value.sourcePath : undefined)
      const destinationPath = await resolveApprovedNewPath(
        value && 'destinationPath' in value ? value.destinationPath : undefined
      )
      const expected = value && 'sourceReceipt' in value ? value.sourceReceipt : undefined

      if (
        !expected ||
        typeof expected !== 'object' ||
        !('path' in expected) ||
        !('dev' in expected) ||
        !('ino' in expected) ||
        !('size' in expected) ||
        !('mtimeMs' in expected) ||
        !('sha256' in expected) ||
        expected.path !== sourcePath
      ) {
        throw new Error('Invalid staged source metadata')
      }
      const current = await sourceReceipt(sourcePath)

      if (!sameReceipt(expected as WorkstationSourceReceipt, current.source)) {
        return { error: 'source-conflict', ok: false }
      }

      try {
        await fs.promises.link(sourcePath, destinationPath)
      } catch (error) {
        const code = (error as NodeJS.ErrnoException).code

        if (code === 'EEXIST') {
          throw new Error('Destination already exists')
        }
        if (code !== 'EXDEV') {
          throw error
        }
        await fs.promises.copyFile(sourcePath, destinationPath, fs.constants.COPYFILE_EXCL)
      }

      try {
        const immediatelyBeforeMove = await sourceReceipt(sourcePath)

        if (!sameReceipt(current.source, immediatelyBeforeMove.source)) {
          await fs.promises.rm(destinationPath, { force: true })

          return { error: 'source-conflict', ok: false }
        }
        await fs.promises.unlink(sourcePath)
      } catch (error) {
        await fs.promises.rm(destinationPath, { force: true })
        throw error
      }
      const moved = await sourceReceipt(destinationPath)

      return { ok: true, source: moved.source }
    }

    if (op === 'trash') {
      const requestedPath = payload && typeof payload === 'object' && 'path' in payload ? payload.path : undefined
      const targetPath = await resolveApprovedPath(requestedPath)
      const approvedRoots = Object.values(await canonicalRoots())

      if (approvedRoots.includes(targetPath)) {
        throw new Error('Approved workstation roots cannot be trashed')
      }
      await trashItem(targetPath)

      return { ok: true, path: targetPath }
    }

    throw new Error('Unsupported workstation folder operation')
  }
}

export function registerFsIpc({
  hermesHome,
  readActiveDesktopProfile,
  expandUserPath,
  resolveRequestedPathForIpc,
  directoryExists,
  resolveGitBinary
}: FsIpcDeps) {
  const workstationFolders = createWorkstationFoldersHandler({
    plane: managedProductPlane,
    roots: {
      desktop: app.getPath('desktop'),
      documents: app.getPath('documents'),
      downloads: app.getPath('downloads')
    },
    trashItem: targetPath => shell.trashItem(targetPath)
  })

  ipcMain.handle('hermes:workstationFolders', async (_event, op, payload) => workstationFolders(op, payload))

  ipcMain.handle('hermes:fs:readDir', async (_event, dirPath) => readDirForIpc(dirPath))

  ipcMain.handle('hermes:fs:gitRoot', async (_event, startPath) => gitRootForIpc(startPath))

  // Reveal a path in the OS file manager (Finder / Explorer / Files).
  ipcMain.handle('hermes:fs:reveal', async (_event, targetPath) => {
    const target = String(targetPath || '').trim()

    if (!target) {
      return false
    }

    try {
      shell.showItemInFolder(target)

      return true
    } catch {
      return false
    }
  })

  // Open a DIRECTORY in the OS file manager, creating it first if needed. Unlike
  // `reveal` (which selects an existing item and silently no-ops on a missing
  // path — the "Open plugins folder" Windows bug), this is for the plugins door,
  // which often doesn't exist on first use. `shell.openPath` returns '' on
  // success or an error string; both mkdir + openPath failures are surfaced.
  ipcMain.handle('hermes:fs:openDir', async (_event, dirPath) => {
    const dir = String(dirPath || '').trim()

    if (!dir) {
      return { ok: false, error: 'no path' }
    }

    try {
      await fs.promises.mkdir(dir, { recursive: true })
      const error = await shell.openPath(path.normalize(dir))

      return error ? { ok: false, error } : { ok: true }
    } catch (error) {
      return { ok: false, error: error instanceof Error ? error.message : String(error) }
    }
  })

  // The LOCAL Desktop runtime-plugin root: `<HERMES_HOME>/desktop-plugins`,
  // resolved from the main-process HERMES_HOME (see resolveHermesHome) — NOT from
  // the connected backend. A remote backend reports its own `hermes_home` over
  // the gateway, which is a path on the REMOTE box; deriving the plugin dir from
  // it yields `undefined/desktop-plugins` (or a non-existent remote path) and the
  // on-disk plugin door silently breaks (#66899). Electron owns this resolution
  // so it stays valid in every connection mode. Created on demand, like openDir.
  async function localPluginsRoot(dirName: string): Promise<string> {
    // Profile-aware: a named Desktop profile gets its own plugin root under
    // profiles/<name>/, matching the profile-scoped hermes_home the backend
    // reported before this resolver existed. 'default'/unset pins the global root.
    const profile = readActiveDesktopProfile()
    const base = profile && profile !== 'default' ? path.join(hermesHome, 'profiles', profile) : hermesHome
    const dir = path.join(base, dirName)

    try {
      await fs.promises.mkdir(dir, { recursive: true })
    } catch {
      // Best-effort create; return the path regardless so the reveal action can
      // still surface a real openPath error and the scanner can retry later.
    }

    return dir
  }

  ipcMain.handle('hermes:fs:desktopPluginsRoot', async () => localPluginsRoot('desktop-plugins'))

  // The LOCAL logs root (`<HERMES_HOME>/logs`, profile-aware) — the error
  // card's "Open Logs" action reveals agent.log/gateway.log without the user
  // knowing where HERMES_HOME lives. Same Electron-local resolution as the
  // plugin roots: valid in every connection mode, created on demand.
  ipcMain.handle('hermes:fs:logsRoot', async () => localPluginsRoot('logs'))

  // The LOCAL agent-plugin root (`<HERMES_HOME>/plugins`), same Electron-local
  // resolution as above. This is the desktop half of a UNIFIED plugin package:
  // an agent plugin may ship `desktop/plugin.js` alongside its Python code (the
  // same shape as `dashboard/manifest.json`), and the renderer's disk door scans
  // this root for it — one installable folder serving both SDKs.
  ipcMain.handle('hermes:fs:agentPluginsRoot', async () => localPluginsRoot('plugins'))

  ipcMain.handle('hermes:plugin:probe', async (_event, payload) => {
    const identifier = String(payload?.identifier || payload?.repo || '').trim()

    if (!identifier) {
      return { ok: false, error: 'identifier is required', agent: false, desktop: false, warnings: [] }
    }

    return probePluginRepo(resolveGitBinary(), identifier)
  })

  ipcMain.handle('hermes:plugin:installDesktop', async (_event, payload) => {
    const identifier = String(payload?.identifier || payload?.repo || '').trim()

    if (!identifier) {
      return { ok: false, error: 'identifier is required' }
    }

    const desktopPluginsRoot = await localPluginsRoot('desktop-plugins')

    return installDesktopPluginFromGit(resolveGitBinary(), identifier, desktopPluginsRoot, Boolean(payload?.force))
  })

  // Rename a file/folder in place. The renderer passes the existing path + a new
  // base name; the destination is resolved in the SAME parent dir so a rename can
  // never move the item elsewhere or traverse out. Rejects on a name collision.
  ipcMain.handle('hermes:fs:rename', async (_event, targetPath, newName) => {
    const src = String(targetPath || '').trim()
    const name = String(newName || '').trim()

    if (!src || !name || name === '.' || name === '..' || name.includes('/') || name.includes('\\')) {
      throw new Error('Invalid rename')
    }

    const dst = path.join(path.dirname(src), name)

    if (dst === src) {
      return { path: dst }
    }

    if (fs.existsSync(dst)) {
      throw new Error(`"${name}" already exists`)
    }

    await fs.promises.rename(src, dst)

    return { path: dst }
  })

  // Write a small UTF-8 text file (e.g. a project's IDEA.md at creation). The path
  // is hardened (resolveRequestedPathForIpc) and the parent must already exist —
  // this never creates directory trees or escapes the allowed roots, and content
  // is size-capped so it can't be abused as a bulk-write primitive.
  ipcMain.handle('hermes:fs:writeText', async (_event, filePath, content) => {
    const raw = String(filePath || '').trim()

    if (!raw) {
      throw new Error('Invalid path')
    }

    const text = String(content ?? '')

    if (text.length > 1_000_000) {
      throw new Error('Content too large')
    }

    const resolved = resolveRequestedPathForIpc(expandUserPath(raw), { purpose: 'Write text file' })

    if (!directoryExists(path.dirname(resolved))) {
      throw new Error('Parent directory does not exist')
    }

    await fs.promises.writeFile(resolved, text, 'utf8')

    return { path: resolved }
  })

  // Move a file/folder to the OS trash (recoverable) — the VS Code "Delete"
  // default. `shell.trashItem` routes to Finder/Explorer/Files trash per platform.
  ipcMain.handle('hermes:fs:trash', async (_event, targetPath) => {
    const target = String(targetPath || '').trim()

    if (!target) {
      throw new Error('Invalid delete')
    }

    await shell.trashItem(target)

    return true
  })
}
