// Vite's ambient client types. Needed for the side-effect `import './styles.css'`
// in main.tsx: TypeScript 7 rejects side-effect imports with no declaration (TS2882),
// and vite/client is what declares the CSS/asset module shapes.
/// <reference types="vite/client" />
