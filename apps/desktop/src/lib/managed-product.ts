declare const __HERMES_DESKTOP_BUILD_PRODUCT__: 'internal' | 'external' | null

export const managedProductPlane =
  typeof __HERMES_DESKTOP_BUILD_PRODUCT__ === 'string' ? __HERMES_DESKTOP_BUILD_PRODUCT__ : null
export const isManagedProductBuild = managedProductPlane !== null
