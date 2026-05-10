import { describe, it, expect, beforeEach } from 'vitest'
import {
  DEFAULT_COLLAPSED,
  getCollapsed,
  setCollapsed,
} from './collapseStore'

describe('collapseStore', () => {
  beforeEach(() => {
    localStorage.clear()
  })

  it('round-trips a collapsed value', () => {
    setCollapsed('user-a', 'allergies', true)
    expect(getCollapsed('user-a', 'allergies')).toBe(true)
    setCollapsed('user-a', 'allergies', false)
    expect(getCollapsed('user-a', 'allergies')).toBe(false)
  })

  it('namespaces keys by userKey — user-a setting does not affect user-b', () => {
    setCollapsed('user-a', 'allergies', true)
    expect(getCollapsed('user-b', 'allergies')).toBe(DEFAULT_COLLAPSED)
  })

  it('namespaces keys by cardId — allergies setting does not affect problems', () => {
    setCollapsed('user-a', 'allergies', true)
    expect(getCollapsed('user-a', 'problems')).toBe(DEFAULT_COLLAPSED)
  })

  it('returns the default when the key is missing', () => {
    expect(getCollapsed('user-a', 'allergies')).toBe(DEFAULT_COLLAPSED)
  })

  it('returns the default when the stored value is corrupt', () => {
    // Two writes that should never come from setCollapsed: a non-binary
    // string and a JSON-serialized boolean. Both must read as the default
    // so a bad migration / dev-tools edit never pins a card collapsed.
    localStorage.setItem(
      'dashboard-spa:collapse:user-a:allergies',
      'collapsed',
    )
    expect(getCollapsed('user-a', 'allergies')).toBe(DEFAULT_COLLAPSED)
    localStorage.setItem('dashboard-spa:collapse:user-a:allergies', 'true')
    expect(getCollapsed('user-a', 'allergies')).toBe(DEFAULT_COLLAPSED)
  })

  it('writes under the documented PREFIX:userKey:cardId key shape', () => {
    setCollapsed('user-42', 'lab-results', true)
    expect(
      localStorage.getItem('dashboard-spa:collapse:user-42:lab-results'),
    ).toBe('1')
  })
})
