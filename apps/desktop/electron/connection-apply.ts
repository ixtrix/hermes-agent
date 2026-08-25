async function applyConnectionChange({
  cancelAndWait,
  isPrimary,
  managed = false,
  rehomePrimary = null,
  scope,
  sendApplied,
  stopPool,
  teardownPrimary,
  teardownSsh
}) {
  assertManagedConnectionMutable(managed)

  await cancelAndWait(scope)
  await teardownSsh(scope)

  if (!isPrimary) {
    stopPool(scope)

    return
  }

  if (rehomePrimary) {
    await rehomePrimary()

    return
  }

  await teardownPrimary()
  sendApplied()
}

function assertManagedConnectionMutable(managed) {
  if (managed) {
    throw new Error('Managed product connection is immutable.')
  }
}

function commitConnectionFailure(current, starting, commit) {
  if (current !== starting) {
    return false
  }

  commit()

  return true
}

async function resolveTerminalConnection(getTarget, ensureBackend) {
  let target = getTarget()

  if (target !== 'pending') {
    return target
  }

  await ensureBackend()
  target = getTarget()

  if (target === 'pending') {
    throw new Error('Remote connection is not ready yet. Try again in a moment.')
  }

  return target
}

export { applyConnectionChange, assertManagedConnectionMutable, commitConnectionFailure, resolveTerminalConnection }
