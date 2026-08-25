declare const __HERMES_DESKTOP_BUILD_PRODUCT__: 'internal' | 'external' | null

export const isManagedProductBuild = typeof __HERMES_DESKTOP_BUILD_PRODUCT__ === 'string'
