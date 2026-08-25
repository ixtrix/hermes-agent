
if (__HERMES_DESKTOP_BUILD_PRODUCT__) {
  void import('./managed-main')
} else {
  void import('./ordinary-main')
}
