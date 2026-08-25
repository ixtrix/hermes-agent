import assert from 'node:assert/strict'

import { test } from 'vitest'

import { managedConnectionMatches, modeIsRemoteLike } from './connection-config'

test('managed connection matching accepts only the fixed remote tuple', () => {
  const policy = {
    oidc: { audience: 'aud', issuer: 'https://login.example.test', redirectUri: 'https://app.example.test/callback' },
    origin: 'https://hermes.example.test',
    plane: 'internal',
    productId: 'uk.co.scopefurnishing.hermes.internal',
    productName: 'Scope Hermes Internal'
  }

  assert.equal(managedConnectionMatches(policy, { origin: policy.origin, plane: policy.plane, productId: policy.productId }), true)
  assert.equal(managedConnectionMatches(policy, { origin: 'http://localhost:9119', plane: 'internal', productId: policy.productId }), false)
  assert.equal(modeIsRemoteLike('remote'), true)
  assert.equal(modeIsRemoteLike('local'), false)
})
