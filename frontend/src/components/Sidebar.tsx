/**
 * Configurable navigation sidebar.
 *
 * Vendored from @disciplin-run/shared-ui (sibling monorepo package) so this
 * frontend stands alone in CI without checking out the parent monorepo.
 * Pull updates from the source manually if its API or behavior changes.
 *
 * Usage:
 *   <Sidebar
 *     title="tubemail"
 *     tabs={[
 *       { id: 'workers', label: 'Workers' },
 *       { id: 'permissions', label: 'Permissions', badge: 3 },
 *     ]}
 *     bottomTabs={[{ id: 'settings', label: 'Settings' }]}
 *     activeTab={tab}
 *     onTabChange={setTab}
 *   />
 */

import { useState, useEffect } from 'react'

export interface SidebarTab {
  id: string
  label: string
  /** Optional count badge (e.g. pending items). Hidden when undefined or 0. */
  badge?: number
}

export interface SidebarProps {
  /** Service name shown in the header */
  title: string
  /** Main navigation tabs */
  tabs: SidebarTab[]
  /** Tabs pinned to the bottom (e.g. Settings) */
  bottomTabs?: SidebarTab[]
  /** Currently active tab ID */
  activeTab: string
  /** Called when a tab is clicked */
  onTabChange: (tabId: string) => void
}

export function Sidebar({
  title, tabs, bottomTabs = [], activeTab, onTabChange,
}: SidebarProps) {
  const [version, setVersion] = useState('')

  useEffect(() => {
    fetch('/health')
      .then((r) => r.json())
      .then((d) => setVersion(d.version || ''))
      .catch((err) => console.error('Sidebar /health fetch failed:', err))
  }, [])

  return (
    <nav className="sidebar">
      <div className="sidebar-brand">{title}</div>
      <div className="sidebar-nav">
        {tabs.map((t) => (
          <div
            key={t.id}
            className={`sidebar-item ${activeTab === t.id ? 'active' : ''}`}
            onClick={() => onTabChange(t.id)}
          >
            <span>{t.label}</span>
            {t.badge !== undefined && t.badge > 0 && (
              <span className="sidebar-badge">{t.badge}</span>
            )}
          </div>
        ))}
        <div className="sidebar-spacer" />
        {bottomTabs.map((t) => (
          <div
            key={t.id}
            className={`sidebar-item ${activeTab === t.id ? 'active' : ''}`}
            onClick={() => onTabChange(t.id)}
          >
            <span>{t.label}</span>
            {t.badge !== undefined && t.badge > 0 && (
              <span className="sidebar-badge">{t.badge}</span>
            )}
          </div>
        ))}
        {version && (
          <div
            style={{
              padding: '0.5rem 1rem',
              fontSize: '0.7rem',
              color: 'var(--gray-400)',
            }}
          >
            v{version}
          </div>
        )}
      </div>
    </nav>
  )
}
