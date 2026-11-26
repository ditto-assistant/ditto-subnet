import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'
import { ScreeningError } from './ScreeningRouteFrame'

describe('ScreeningError', () => {
  it('shows an Error message and handles non-Error throws safely', () => {
    expect(renderToStaticMarkup(<ScreeningError error={new Error('Unavailable')} />)).toContain('Unavailable')
    expect(renderToStaticMarkup(<ScreeningError error={null} />)).toContain('An unexpected error occurred')
  })
})
