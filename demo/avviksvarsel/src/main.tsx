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

import App from './App';
import { reportClientError } from './api';
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

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
