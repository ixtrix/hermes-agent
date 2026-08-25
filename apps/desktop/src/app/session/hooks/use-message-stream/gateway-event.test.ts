import assert from 'node:assert/strict'

import { test } from 'vitest'

import { shouldRejectManagedBridgeEvent } from './gateway-event'

test('managed builds reject callback and app-control bridge events', () => {
  assert.equal(shouldRejectManagedBridgeEvent('callback:invoke', true), true)
  assert.equal(shouldRejectManagedBridgeEvent('app-control:open', true), true)
})

test('ordinary chat events remain allowed on managed builds', () => {
  assert.equal(shouldRejectManagedBridgeEvent('chat.message', true), false)
  assert.equal(shouldRejectManagedBridgeEvent('session.updated', true), false)
  assert.equal(shouldRejectManagedBridgeEvent('callback:invoke', false), false)
})
