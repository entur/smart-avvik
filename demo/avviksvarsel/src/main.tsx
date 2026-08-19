import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';

// Entur Linje design system. Token chain first (base.css pulls the primitive + semantic colours;
// styles.css adds spacing/radius/etc.), then each component's stylesheet. All pure CSS custom
// properties + component CSS with no remote fonts, so the built bundle is fully self-contained.
import '@entur/tokens/dist/base.css';
import '@entur/tokens/dist/styles.css';
import '@entur/typography/dist/styles.css';
import '@entur/layout/dist/styles.css';
import '@entur/button/dist/styles.css';
import '@entur/form/dist/styles.css';
import '@entur/dropdown/dist/styles.css';
import '@entur/chip/dist/styles.css';
import '@entur/alert/dist/styles.css';
import '@entur/modal/dist/styles.css';
import '@entur/menu/dist/styles.css';
import '@entur/a11y/dist/styles.css';
import '@entur/loader/dist/styles.css';

// Brand overrides LAST — they win on specificity (`:root[data-brand=…]`), but only once the rules
// they override have been defined. Every brand but Entur has a sheet here; Entur is Linje as
// shipped, so it has none, and a page with no `brand` parameter loads nothing extra.
import './brands/none.css';

import App from './App';
import { reportClientError } from './api';
import { applyBrand, resolveBrand } from './brand';
import './styles.css';

// Uncaught exceptions and promise rejections are forwarded to the server log (best-effort,
// throttled — see api.ts) so client-side failures are visible without a connected devtools.
window.addEventListener('error', (event) => {
  reportClientError('window', event.message, event.error instanceof Error ? event.error.stack : undefined);
});
window.addEventListener('unhandledrejection', (event) => {
  const reason: unknown = event.reason;
  reportClientError(
    'unhandledrejection',
    reason instanceof Error ? reason.message : String(reason),
    reason instanceof Error ? reason.stack : undefined,
  );
});

// index.html has already set data-brand from the same parameter, so the palette is painted; this
// repoints the head assets (manifest, favicons, theme colour) that only the registry knows about.
const brand = resolveBrand();
applyBrand(brand);

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <App brand={brand} />
  </StrictMode>,
);
