# ServerFS website

The project website is a fully static Astro + Starlight site intended for GitHub Pages.

## Stack

- Astro 7
- Starlight
- self-hosted Noto Sans SC Variable for deterministic Simplified Chinese glyphs
- plain CSS plus a tiny dependency-free browser script for viewport reveal and scroll-state motion
- GitHub Pages via `.github/workflows/pages.yml`

No SSR, database, serverless runtime or third-party hosting service is required.

## Languages

- English is the root/default locale: `/ServerFS_MCP/`
- Simplified Chinese is served at: `/ServerFS_MCP/zh-cn/`
- Starlight provides locale-aware docs routing and its built-in Chinese UI translations.
- The custom landing page uses one shared Astro component with a locale-specific copy dictionary.

## Local development

Requires Node.js 22.22.0 or newer, as declared in `package.json`. Use the locked dependencies (`npm ci`); the website package version is independent of the ServerFS release.

```bash
cd site
npm ci
npm run dev
```

Production build:

```bash
npm run build
```

The GitHub Pages project path is configured as `/ServerFS_MCP/` with trailing slashes in `astro.config.mjs`. Use `npm run preview` for the built site. Verify both locale landing pages and all bilingual docs/navigation targets under that base path, the custom 404 page, local assets and external links. Check narrow-screen, keyboard navigation and reduced-motion behavior. A build does not publish the site or validate live ServerFS endpoints.

## Structure

```text
site/
├── public/
├── src/
│   ├── components/           # shared landing-page components
│   ├── content/
│   │   ├── docs/             # root English docs + zh-cn translations
│   │   └── i18n/             # Starlight UI translation overrides
│   ├── pages/                # English root + zh-cn landing routes
│   └── styles/
├── astro.config.mjs
└── package.json
```

The landing page intentionally uses no React island or animation library. Motion is implemented with CSS plus a native browser script for viewport reveals, Hero scroll state and platform navigation; reduced-motion behavior is separate. Add a UI dependency only when a concrete interaction justifies it.
