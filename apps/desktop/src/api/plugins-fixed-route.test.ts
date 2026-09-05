import { afterEach, describe, expect, it, vi } from 'vitest'

import type { PluginProfileRoute } from '@/sdk'

import { setApiRequestConnection, setApiRequestProfile } from './client'
import { pluginRestFor } from './plugins'

const internal: PluginProfileRoute = {
  connectionId: 'internal-id',
  mode: 'remote',
  profile: 'default',
  revision: 'revision-a',
  targetProfile: 'internal'
}

function bridge(routes: PluginProfileRoute[] = [internal]) {
  const api = vi.fn().mockResolvedValue({ ok: true })
  const getProfileRoutes = vi.fn().mockResolvedValue(routes)
  Object.defineProperty(window, 'hermesDesktop', { configurable: true, value: { api, getProfileRoutes } })

  return { api, getProfileRoutes }
}

afterEach(() => {
  setApiRequestConnection(null)
  setApiRequestProfile(null)
  Reflect.deleteProperty(window, 'hermesDesktop')
})

describe('fixed plugin REST owner', () => {
  it('keeps the captured owner when foreground switches during registry lookup', async () => {
    const { api, getProfileRoutes } = bridge()
    getProfileRoutes.mockImplementation(async () => {
      setApiRequestConnection('external-id')
      setApiRequestProfile('web')

      return [internal, { ...internal, connectionId: 'external-id' }]
    })
    await pluginRestFor('scope-mail', internal, '/v2/read', { method: 'POST', body: { binding: 'bound' } })
    expect(api).toHaveBeenCalledExactlyOnceWith(
      expect.objectContaining({
        connectionId: 'internal-id',
        profile: 'default',
        path: '/api/plugins/scope-mail/v2/read',
        expectedConnectionRevision: 'revision-a',
        method: 'POST',
        body: { binding: 'bound' }
      })
    )
  })

  it('keeps an explicit local owner while remote is primary', async () => {
    const local: PluginProfileRoute = { ...internal, connectionId: 'local', mode: 'local', targetProfile: 'default' }
    const { api } = bridge([local])
    setApiRequestConnection('external-id')
    await pluginRestFor('scope-mail', local, '/v2/capabilities')
    expect(api).toHaveBeenCalledWith(expect.objectContaining({ connectionId: 'local', profile: 'default' }))
  })

  it.each([
    { routes: [] },
    { routes: [{ ...internal, connectionId: 'replacement' }] },
    { routes: [{ ...internal, targetProfile: 'web' }] },
    { routes: [{ ...internal, mode: 'local' as const }] },
    { routes: [{ ...internal, revision: 'revision-b' }] }
  ])('refuses removed or replaced registry descriptors before dispatch', async ({ routes }) => {
    const { api } = bridge(routes)
    await expect(pluginRestFor('scope-mail', internal, '/v2/read')).rejects.toThrow('no longer registered')
    expect(api).not.toHaveBeenCalled()
  })

  it.each([
    '/../config',
    '/%2e%2e/config',
    '/%252e%252e/config',
    '/x\\..\\config',
    '/read?profile=web',
    '/\n../config',
    '/v2/read?pro\nfile=web'
  ])('refuses namespace or owner escape: %s', async path => {
    const { api } = bridge()
    await expect(pluginRestFor('scope-mail', internal, path)).rejects.toThrow()
    expect(api).not.toHaveBeenCalled()
  })

  it('preserves upload bytes, timeout and encoded resource IDs', async () => {
    const { api } = bridge()
    const upload = { filename: 'test.txt', bytes: new Uint8Array([1, 2, 3]).buffer }
    await pluginRestFor('scope-mail', internal, '/files/a%2Fb', { upload, timeoutMs: 42 })
    expect(api).toHaveBeenCalledWith(
      expect.objectContaining({ upload, timeoutMs: 42, path: '/api/plugins/scope-mail/files/a%2Fb' })
    )
  })

  it('requires a route revision before registry lookup', async () => {
    const { api, getProfileRoutes } = bridge()
    const legacy = { ...internal, revision: undefined }
    await expect(pluginRestFor('scope-mail', legacy, '/v2/read')).rejects.toThrow('revision required')
    expect(getProfileRoutes).not.toHaveBeenCalled()
    expect(api).not.toHaveBeenCalled()
  })

  it('does not fall back when the registry cannot be read', async () => {
    const { api, getProfileRoutes } = bridge()
    getProfileRoutes.mockRejectedValue(new Error('registry unavailable'))
    await expect(pluginRestFor('scope-mail', internal, '/v2/read')).rejects.toThrow('registry unavailable')
    expect(api).not.toHaveBeenCalled()
  })
})
