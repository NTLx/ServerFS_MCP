# ServerFS website

The project website is a fully static Astro + Starlight site intended for GitHub Pages.

## Stack

- Astro 7
- Starlight
- plain CSS for the landing-page visual system and motion
- GitHub Pages via `.github/workflows/pages.yml`

No SSR, database, serverless runtime or third-party hosting service is required.

## Local development

Requires Node.js 22.12 or newer.

```bash
cd site
npm ci
npm run dev
```

Production build:

```bash
npm run build
```

The GitHub Pages project path is configured as `/ServerFS_MCP/` in `astro.config.mjs`.

## Structure

```text
site/
├── public/
├── src/
│   ├── content/docs/docs/   # Starlight pages mounted at /docs/
│   ├── pages/index.astro    # custom project landing page
│   └── styles/
├── astro.config.mjs
└── package.json
```

The landing page intentionally uses no React island or animation library in the initial version. Motion is CSS-only and respects `prefers-reduced-motion`. Add a UI dependency only when a concrete interaction justifies it.
