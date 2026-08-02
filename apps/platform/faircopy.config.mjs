import { astro } from '@faircopy/astro'

// Static HTML is valid Astro syntax, so the official adapter can extract its prose.
const html = { ...astro(), extensions: ['.html'] }

export default {
  files: ['dashboard/index.html'],
  adapters: [html],
  rules: {
    'no-em-dash': 'error',
    'no-weasel-words': 'error',
    'no-rhetorical-scaffolding': 'error',
  },
}
