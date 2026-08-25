import path from 'node:path'

export type ManagedProductPlane = 'external' | 'internal'
export interface ManagedProductPolicy {
  oidc: {
    audience: string
    issuer: string
    redirectUri: string
  }
  origin: string
  plane: ManagedProductPlane
  productId: string
  productName: string
}

export const MANAGED_PRODUCT_IDS = Object.freeze({
  external: 'uk.co.scopefurnishing.hermes.external',
  internal: 'uk.co.scopefurnishing.hermes.internal'
})

export const MANAGED_PRODUCT_NAMES = Object.freeze({
  external: 'Scope Hermes External',
  internal: 'Scope Hermes Internal'
})

export const MANAGED_PRODUCT_EXECUTABLES = Object.freeze({
  external: 'ScopeHermesExternal',
  internal: 'ScopeHermesInternal'
})

export const MANAGED_PRODUCT_PROTOCOLS = Object.freeze({
  external: 'scope-hermes-external',
  internal: 'scope-hermes-internal'
})

export function managedUserDataRoot(
  appData: string | undefined,
  plane: ManagedProductPlane,
  platform: string
): string {
  if (platform === 'win32' && !appData?.trim()) {
    throw new Error('Managed Windows desktop requires LOCALAPPDATA.')
  }
  if (!appData?.trim()) {
    throw new Error('Managed desktop user-data root is unavailable.')
  }
  const base = appData.replace(/[\\/]+$/, '')
  const productRoot = `Scope AI/Hermes ${plane[0].toUpperCase()}${plane.slice(1)}`
  const join = platform === 'win32' ? path.win32.join : path.posix.join

  return join(base, ...productRoot.split('/'))
}

declare const __HERMES_DESKTOP_BUILD_PRODUCT__: ManagedProductPlane | null
declare const __HERMES_DESKTOP_MANAGED_ORIGIN__: string | null
declare const __HERMES_DESKTOP_MANAGED_OIDC_ISSUER__: string | null
declare const __HERMES_DESKTOP_MANAGED_OIDC_AUDIENCE__: string | null
declare const __HERMES_DESKTOP_MANAGED_OIDC_REDIRECT_URI__: string | null

export const managedProductPlane: ManagedProductPlane | null =
  typeof __HERMES_DESKTOP_BUILD_PRODUCT__ === 'string' ? __HERMES_DESKTOP_BUILD_PRODUCT__ : null

const managedProductId = managedProductPlane ? MANAGED_PRODUCT_IDS[managedProductPlane] : null
const managedProductName = managedProductPlane ? MANAGED_PRODUCT_NAMES[managedProductPlane] : null

export const managedProductPolicy: ManagedProductPolicy | null =
  managedProductPlane &&
  typeof __HERMES_DESKTOP_MANAGED_ORIGIN__ === 'string' &&
  typeof __HERMES_DESKTOP_MANAGED_OIDC_ISSUER__ === 'string' &&
  typeof __HERMES_DESKTOP_MANAGED_OIDC_AUDIENCE__ === 'string' &&
  typeof __HERMES_DESKTOP_MANAGED_OIDC_REDIRECT_URI__ === 'string'
    ? Object.freeze({
        oidc: Object.freeze({
          audience: __HERMES_DESKTOP_MANAGED_OIDC_AUDIENCE__,
          issuer: __HERMES_DESKTOP_MANAGED_OIDC_ISSUER__,
          redirectUri: __HERMES_DESKTOP_MANAGED_OIDC_REDIRECT_URI__
        }),
        origin: __HERMES_DESKTOP_MANAGED_ORIGIN__,
        plane: managedProductPlane,
        productId: managedProductId!,
        productName: managedProductName!
      })
    : null

export const isManagedProductBuild = managedProductPlane !== null

if (isManagedProductBuild && !managedProductPolicy) {
  throw new Error('Managed Hermes desktop build is missing its embedded product policy.')
}

