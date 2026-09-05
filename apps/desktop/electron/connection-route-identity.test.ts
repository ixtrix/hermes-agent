import { describe, expect, it } from 'vitest'

import type { RegistryConnection } from './connection-registry'
import { connectionRouteRevision, connectionRouteRevisionMatches } from './connection-route-identity'

const secret = Buffer.from('fixed-test-route-revision-key')

const source: RegistryConnection = {
  authMode: 'token',
  headers: { 'CF-Access-Client-Secret': { encoding: 'safeStorage', value: 'header-a' } },
  id: 'office',
  kind: 'remote',
  label: 'Office',
  token: { encrypted: 'token-a' },
  url: 'https://gateway.example'
}

describe('connection route revisions', () => {
  it('binds source id and complete dial/auth material while ignoring the display label', () => {
    const revision = connectionRouteRevision(source, secret)
    const renamed: RegistryConnection = { ...source, label: 'Renamed office' }

    expect(connectionRouteRevision(renamed, secret)).toBe(revision)
    expect(connectionRouteRevision({ ...source, id: 'backup' }, secret)).not.toBe(revision)
    expect(connectionRouteRevision({ ...source, url: 'https://replacement.example' }, secret)).not.toBe(revision)
    expect(connectionRouteRevision({ ...source, token: { encrypted: 'token-b' } }, secret)).not.toBe(revision)
    expect(
      connectionRouteRevision(
        { ...source, headers: { 'CF-Access-Client-Secret': { encoding: 'safeStorage', value: 'header-b' } } },
        secret
      )
    ).not.toBe(revision)
  })

  it('rejects a cached old descriptor after the current source is rebound', () => {
    const oldRevision = connectionRouteRevision(source, secret)
    const rebound = { ...source, url: 'https://replacement.example' }

    expect(connectionRouteRevisionMatches(oldRevision, oldRevision, rebound, secret)).toBe(false)
  })

  it('rejects a resolved descriptor from another coalesced dial', () => {
    const expected = connectionRouteRevision(source, secret)

    expect(connectionRouteRevisionMatches(expected, 'another-revision', source, secret)).toBe(false)
    expect(connectionRouteRevisionMatches(expected, expected, source, secret)).toBe(true)
  })

  it('preserves compatibility when no revision is expected and gives local an explicit identity', () => {
    const local: RegistryConnection = { id: 'local', kind: 'local', label: 'This device' }

    expect(connectionRouteRevisionMatches(undefined, undefined, source, secret)).toBe(true)
    expect(connectionRouteRevision(local, secret)).toEqual(expect.any(String))
  })

  it('preserves case-sensitive SSH usernames', () => {
    const alice: RegistryConnection = {
      host: 'gateway.example',
      id: 'ssh-office',
      kind: 'ssh',
      label: 'SSH office',
      user: 'Alice'
    }

    const lowerAlice: RegistryConnection = { ...alice, user: 'alice' }

    expect(connectionRouteRevision(alice, secret)).not.toBe(connectionRouteRevision(lowerAlice, secret))
  })
})
