import { type ReactNode, useEffect, useMemo, useState } from 'react'

import { ErrorBoundary } from '@/components/error-boundary'
import { ContribRender } from '@/contrib/react/boundary'
import { useContributions } from '@/contrib/react/use-contributions'
import type { Contribution } from '@/contrib/types'
import { cn } from '@/lib/utils'

export const SECONDARY_SIDEBAR_AREA = 'secondarySidebar'
export const SECONDARY_SIDEBAR_ACTIVATE_EVENT = 'hermes:secondary-sidebar:activate'

interface SidebarCoreSurface {
  id: string
  render: () => ReactNode
  title: string
}

interface SidebarSurfaceGroupProps {
  core: SidebarCoreSurface
}

interface SidebarSurfaceActivation {
  id: string
}

export function activateSecondarySidebarSurface(id: string): void {
  if (typeof window === 'undefined' || !id.trim()) {
    return
  }

  window.dispatchEvent(new CustomEvent<SidebarSurfaceActivation>(SECONDARY_SIDEBAR_ACTIVATE_EVENT, { detail: { id } }))
}

function contributedSurface(contribution: Contribution): contribution is Contribution & {
  render: () => ReactNode
  title: string
} {
  return typeof contribution.render === 'function' && Boolean(contribution.title?.trim())
}

export function SidebarSurfaceGroup({ core }: SidebarSurfaceGroupProps) {
  const contributions = useContributions(SECONDARY_SIDEBAR_AREA).filter(contributedSurface)

  const surfaces = useMemo(
    () => [core, ...contributions.map(({ id, render, title }) => ({ id, render, title }))],
    [core, contributions]
  )

  const [activeId, setActiveId] = useState(core.id)
  const active = surfaces.find(surface => surface.id === activeId) ?? core

  useEffect(() => {
    if (!surfaces.some(surface => surface.id === activeId)) {
      setActiveId(core.id)
    }
  }, [activeId, core.id, surfaces])

  useEffect(() => {
    const onActivate = (event: Event) => {
      const id = (event as CustomEvent<SidebarSurfaceActivation>).detail?.id

      if (typeof id === 'string' && surfaces.some(surface => surface.id === id)) {
        setActiveId(id)
      }
    }

    window.addEventListener(SECONDARY_SIDEBAR_ACTIVATE_EVENT, onActivate)

    return () => window.removeEventListener(SECONDARY_SIDEBAR_ACTIVATE_EVENT, onActivate)
  }, [surfaces])

  if (contributions.length === 0) {
    return <ContribRender render={core.render} />
  }

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div
        aria-label="Sidebar surfaces"
        className="flex h-8 shrink-0 border-b border-(--ui-stroke-secondary) px-1"
        role="tablist"
      >
        {surfaces.map(surface => {
          const selected = surface.id === active.id

          return (
            <button
              aria-selected={selected}
              className={cn(
                'relative min-w-0 flex-1 truncate px-2 text-[0.6875rem] font-medium transition-colors',
                selected ? 'text-(--ui-text-primary)' : 'text-(--ui-text-tertiary) hover:text-(--ui-text-secondary)'
              )}
              key={surface.id}
              onClick={() => setActiveId(surface.id)}
              role="tab"
              type="button"
            >
              {surface.title}
              {selected && <span className="absolute inset-x-2 bottom-0 h-px bg-(--ui-accent)" />}
            </button>
          )
        })}
      </div>
      <div className="flex min-h-0 flex-1 flex-col" role="tabpanel">
        <ErrorBoundary key={active.id} label={`secondary-sidebar:${active.id}`}>
          <ContribRender render={active.render} />
        </ErrorBoundary>
      </div>
    </div>
  )
}
