import assert from 'node:assert/strict'

import { test, vi } from 'vitest'

const mocks = vi.hoisted(() => ({
  exposeInMainWorld: vi.fn(),
  invoke: vi.fn()
}))

vi.mock('electron', () => ({
  contextBridge: { exposeInMainWorld: mocks.exposeInMainWorld },
  ipcRenderer: {
    invoke: mocks.invoke,
    on: vi.fn(),
    removeListener: vi.fn(),
    send: vi.fn(),
    sendSync: vi.fn()
  },
  webUtils: { getPathForFile: vi.fn() }
}))
vi.mock('./managed-product', () => ({ isManagedProductBuild: true }))

test('managed preload exposes only the remote-safe bridge', async () => {
  await import('./preload')

  const bridge = mocks.exposeInMainWorld.mock.calls[0]?.[1] as Record<string, unknown>
  assert.equal(typeof bridge.getConnection, 'function')
  assert.equal(typeof bridge.getConnectionConfig, 'function')
  assert.equal(typeof bridge.oauthLoginConnectionConfig, 'function')
  assert.equal(typeof bridge.getGatewayWsUrl, 'function')
  assert.equal(typeof bridge.pickAttachmentBytes, 'function')
  assert.equal('selectPaths' in bridge, false)

  await (bridge.pickAttachmentBytes as (options: unknown) => Promise<unknown>)({ purpose: 'document' })
  assert.equal(mocks.invoke.mock.calls.at(-1)?.[0], 'hermes:pickAttachmentBytes')
})
