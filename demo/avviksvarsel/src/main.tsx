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
import './styles.css';

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
