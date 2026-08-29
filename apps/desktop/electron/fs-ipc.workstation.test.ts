import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

import { ipcMain } from 'electron'
import { afterEach, test, vi } from 'vitest'

vi.mock('electron', () => ({
  app: { getPath: () => '' },
  ipcMain: { handle: vi.fn() },
  shell: { openPath: vi.fn(), showItemInFolder: vi.fn(), trashItem: vi.fn() }
}))

import { isEnrolledInternalWorkstationConnection } from './connection-config'
import { createWorkstationFoldersHandler, registerFsIpc } from './fs-ipc'

const rootsToRemove: string[] = []

function fixture() {
  const createdHome = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-workstation-folders-'))
  rootsToRemove.push(createdHome)
  const requestedRoots = {
    desktop: path.join(createdHome, 'Desktop'),
    documents: path.join(createdHome, 'Documents'),
    downloads: path.join(createdHome, 'Downloads')
  }

  Object.values(requestedRoots).forEach(root => fs.mkdirSync(root))
  const roots = {
    desktop: fs.realpathSync(requestedRoots.desktop),
    documents: fs.realpathSync(requestedRoots.documents),
    downloads: fs.realpathSync(requestedRoots.downloads)
  }
  const home = fs.realpathSync(createdHome)
  const trashItem = vi.fn(async () => undefined)
  const invoke = createWorkstationFoldersHandler({ roots: requestedRoots, trashItem })

  return { home, invoke, roots, trashItem }
}

afterEach(() => {
  while (rootsToRemove.length > 0) {
    fs.rmSync(rootsToRemove.pop()!, { recursive: true, force: true })
  }
})

test('workstation IPC fails closed before touching folders when the active connection is unauthorized', async () => {
  const authorizeWorkstationFolders = vi.fn(async () => false)
  const handle = vi.mocked(ipcMain.handle)
  handle.mockClear()
  registerFsIpc({
    authorizeWorkstationFolders,
    directoryExists: () => false,
    expandUserPath: value => value,
    hermesHome: '/hermes',
    readActiveDesktopProfile: () => null,
    resolveGitBinary: () => 'git',
    resolveRequestedPathForIpc: value => value
  })
  const registered = handle.mock.calls.find(([channel]) => channel === 'hermes:workstationFolders')
  assert.ok(registered)

  await assert.rejects(() =>
    Reflect.apply(registered[1], null, [{ sender: { id: 17 } }, 'roots', {}])
  )
  assert.deepEqual(authorizeWorkstationFolders.mock.calls, [[17]])
})

test('workstation policy requires the exact active Internal gateway, not a profile label', () => {
  assert.equal(
    isEnrolledInternalWorkstationConnection({
      authMode: 'oauth',
      baseUrl: 'https://ben.internal.hermes.scopefurnishing.co.uk/instances/a27e539fc517169d8da049ea6e2c8519/',
      mode: 'remote'
    }),
    true
  )
  assert.equal(
    isEnrolledInternalWorkstationConnection({
      authMode: 'oauth',
      baseUrl: 'https://ben.external.hermes.scopefurnishing.co.uk/instances/a27e539fc517169d8da049ea6e2c8519',
      mode: 'remote',
      profile: 'Scope Hermes Internal'
    }),
    false
  )
  assert.equal(
    isEnrolledInternalWorkstationConnection({
      authMode: 'oauth',
      mode: 'remote',
      profile: 'Scope Hermes Internal'
    }),
    false
  )
})

test('roots and list expose only canonical Desktop Documents and Downloads descendants', async t => {
  const { home, invoke, roots } = fixture()
  fs.writeFileSync(path.join(roots.desktop, 'safe.txt'), 'safe')
  fs.writeFileSync(path.join(home, 'outside.txt'), 'outside')

  if (process.platform !== 'win32') {
    fs.symlinkSync(path.join(home, 'outside.txt'), path.join(roots.desktop, 'escape.txt'))
  }

  const rootResult = await invoke('roots', {})
  assert.deepEqual(rootResult, { ok: true, roots })
  const list = await invoke('list', { path: roots.desktop })
  assert.equal(list.ok, true)
  assert.deepEqual(
    list.entries.map(entry => entry.name),
    ['safe.txt']
  )
  await assert.rejects(() => invoke('list', { path: home }), /approved workstation folders/)

  if (process.platform === 'win32') {
    t.skip('symlink containment is covered on POSIX')
  } else {
    await assert.rejects(() => invoke('stage', { path: path.join(roots.desktop, 'escape.txt') }), /approved/)
  }
})

test('stage returns bounded bytes and source metadata used by explicit conflict-safe save', async () => {
  const { invoke, roots } = fixture()
  const sourcePath = path.join(roots.documents, 'draft.txt')
  fs.writeFileSync(sourcePath, 'first')

  const staged = await invoke('stage', { path: sourcePath })
  assert.equal(staged.ok, true)
  assert.equal(Buffer.from(staged.contentBase64, 'base64').toString(), 'first')
  assert.equal(staged.source.path, sourcePath)
  assert.equal(staged.source.sha256.length, 64)

  fs.writeFileSync(sourcePath, 'changed elsewhere')
  const conflict = await invoke('save', {
    contentBase64: Buffer.from('agent edit').toString('base64'),
    source: staged.source
  })
  assert.deepEqual(conflict, { ok: false, error: 'source-conflict' })
  assert.equal(fs.readFileSync(sourcePath, 'utf8'), 'changed elsewhere')
})

test('explicit save updates the same staged source and returns a fresh receipt', async () => {
  const { invoke, roots } = fixture()
  const sourcePath = path.join(roots.downloads, 'notes.txt')
  fs.writeFileSync(sourcePath, 'before')
  const staged = await invoke('stage', { path: sourcePath })

  const saved = await invoke('save', {
    contentBase64: Buffer.from('after').toString('base64'),
    source: staged.source
  })

  assert.equal(saved.ok, true)
  assert.equal(saved.source.path, sourcePath)
  assert.notEqual(saved.source.sha256, staged.source.sha256)
  assert.equal(fs.readFileSync(sourcePath, 'utf8'), 'after')
})

test('trash uses Electron Native Trash and returns only the canonical original path', async () => {
  const { invoke, roots, trashItem } = fixture()
  const sourcePath = path.join(roots.desktop, 'old.txt')
  fs.writeFileSync(sourcePath, 'old')

  const result = await invoke('trash', { path: sourcePath })

  assert.deepEqual(result, { ok: true, path: sourcePath })
  assert.deepEqual(trashItem.mock.calls, [[sourcePath]])
})

test('create explicitly saves a new contained file and fails on collisions', async () => {
  const { invoke, roots } = fixture()
  const targetPath = path.join(roots.documents, 'created.txt')

  const created = await invoke('create', {
    contentBase64: Buffer.from('created').toString('base64'),
    path: targetPath
  })

  assert.equal(created.ok, true)
  assert.equal(created.source.path, targetPath)
  assert.equal(fs.readFileSync(targetPath, 'utf8'), 'created')
  await assert.rejects(
    () => invoke('create', { contentBase64: Buffer.from('again').toString('base64'), path: targetPath }),
    /already exists/
  )
})

test('move conflict-checks its source receipt, stays contained, and never replaces a destination', async () => {
  const { invoke, roots } = fixture()
  const sourcePath = path.join(roots.downloads, 'move-me.txt')
  const destinationPath = path.join(roots.documents, 'moved.txt')
  fs.writeFileSync(sourcePath, 'move me')
  const staged = await invoke('stage', { path: sourcePath })

  const moved = await invoke('move', {
    destinationPath,
    sourcePath,
    sourceReceipt: staged.source
  })

  assert.equal(moved.ok, true)
  assert.equal(moved.source.path, destinationPath)
  assert.equal(fs.existsSync(sourcePath), false)
  assert.equal(fs.readFileSync(destinationPath, 'utf8'), 'move me')

  fs.writeFileSync(sourcePath, 'new source')
  const stale = await invoke('stage', { path: sourcePath })
  fs.writeFileSync(sourcePath, 'changed')
  const conflict = await invoke('move', {
    destinationPath: path.join(roots.desktop, 'other.txt'),
    sourcePath,
    sourceReceipt: stale.source
  })
  assert.deepEqual(conflict, { error: 'source-conflict', ok: false })
})

test('unknown operations and oversized stage files are rejected without a generic filesystem fallback', async () => {
  const { invoke, roots } = fixture()
  const sourcePath = path.join(roots.desktop, 'large.bin')
  fs.writeFileSync(sourcePath, Buffer.alloc(10_000_001))

  await assert.rejects(() => invoke('read', { path: sourcePath }), /Unsupported/)
  await assert.rejects(() => invoke('stage', { path: sourcePath }), /10 MB/)
})
