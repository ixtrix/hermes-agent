import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, it } from 'vitest'

import { registry } from '@/contrib/registry'

import { activateSecondarySidebarSurface, SECONDARY_SIDEBAR_AREA, SidebarSurfaceGroup } from './sidebar-surface-group'

const disposers: Array<() => void> = []

afterEach(() => {
  cleanup()

  while (disposers.length > 0) {
    disposers.pop()?.()
  }
})

it('keeps the core surface unchanged until a plugin contributes a tab', async () => {
  render(<SidebarSurfaceGroup core={{ id: 'files', render: () => <div>Project files</div>, title: 'Files' }} />)

  expect(screen.getByText('Project files')).toBeTruthy()
  expect(screen.queryByRole('tablist')).toBeNull()

  disposers.push(
    registry.register({
      area: SECONDARY_SIDEBAR_AREA,
      id: 'scope-config-files:artifacts',
      render: () => <div>Offline artifact</div>,
      source: 'plugin:scope-config-files',
      title: 'Artifacts'
    })
  )

  const tab = await screen.findByRole('tab', { name: 'Artifacts' })
  fireEvent.click(tab)
  expect(screen.getByText('Offline artifact')).toBeTruthy()
  expect(screen.queryByText('Project files')).toBeNull()
})

it('activates an exact namespaced surface by event and falls back when it unregisters', async () => {
  const dispose = registry.register({
    area: SECONDARY_SIDEBAR_AREA,
    id: 'sample:preview',
    render: () => <div>Preview content</div>,
    source: 'plugin:sample',
    title: 'Preview'
  })

  disposers.push(dispose)

  render(<SidebarSurfaceGroup core={{ id: 'files', render: () => <div>Project files</div>, title: 'Files' }} />)

  activateSecondarySidebarSurface('sample:preview')
  await screen.findByText('Preview content')

  dispose()
  disposers.pop()
  await waitFor(() => expect(screen.getByText('Project files')).toBeTruthy())
  expect(screen.queryByRole('tablist')).toBeNull()
})
