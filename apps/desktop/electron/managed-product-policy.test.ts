import assert from 'node:assert/strict'

import { test } from 'vitest'

import { MANAGED_PRODUCT_IDS, managedProductIdForPlane, resolveManagedProductPolicy } from './connection-config'
import {
  MANAGED_PRODUCT_EXECUTABLES,
  MANAGED_PRODUCT_NAMES,
  MANAGED_PRODUCT_PROTOCOLS,
  managedUserDataRoot
} from './managed-product'

test('managed product IDs are fixed and runtime environment cannot select a policy', () => {
  assert.deepEqual(MANAGED_PRODUCT_IDS, {
    external: 'uk.co.scopefurnishing.hermes.external',
    internal: 'uk.co.scopefurnishing.hermes.internal'
  })
  assert.equal(managedProductIdForPlane('internal'), MANAGED_PRODUCT_IDS.internal)
  assert.equal(managedProductIdForPlane('external'), MANAGED_PRODUCT_IDS.external)
  assert.equal(resolveManagedProductPolicy(), null)
})
test('managed products have distinct identities and deterministic platform roots', () => {
  assert.deepEqual(MANAGED_PRODUCT_NAMES, {
    external: 'Scope Hermes External',
    internal: 'Scope Hermes Internal'
  })
  assert.deepEqual(MANAGED_PRODUCT_EXECUTABLES, {
    external: 'ScopeHermesExternal',
    internal: 'ScopeHermesInternal'
  })
  assert.deepEqual(MANAGED_PRODUCT_PROTOCOLS, {
    external: 'scope-hermes-external',
    internal: 'scope-hermes-internal'
  })
  assert.equal(
    managedUserDataRoot('/Users/example/Library/Application Support', 'internal', 'darwin'),
    '/Users/example/Library/Application Support/Scope AI/Hermes Internal'
  )
  assert.equal(
    managedUserDataRoot('C:\\Users\\example\\AppData\\Local', 'external', 'win32'),
    'C:\\Users\\example\\AppData\\Local\\Scope AI\\Hermes External'
  )
  assert.throws(
    () => managedUserDataRoot(undefined, 'internal', 'win32'),
    /LOCALAPPDATA/
  )
})
