/** Local type declarations for `@ai-agents/shared-ui`.
 *
 * The shared component package lives in a sibling directory at
 * `../../shared/frontend/src` and is wired through Vite's `resolve.alias`
 * for runtime, but the ecosystem doesn't ship .d.ts files alongside it.
 * tsc following the path mapping into shared/frontend/src complains
 * because shared/ has no react types installed at that path.
 *
 * This shim declares only the types tubemail actually uses, which is
 * enough to keep the type-check happy without having to fix the
 * ecosystem-wide setup. iris-qa and leanspecs hit the same issue —
 * tracked separately.
 */
declare module '@ai-agents/shared-ui' {
  import type { ReactNode } from 'react'

  export interface SidebarTab {
    id: string
    label: string
    /** Optional count badge. Hidden when undefined or 0. */
    badge?: number
  }

  export interface SidebarProps {
    title: string
    tabs: SidebarTab[]
    bottomTabs?: SidebarTab[]
    activeTab: string
    onTabChange: (tabId: string) => void
  }

  export const Sidebar: (props: SidebarProps) => ReactNode
}
