import { defineConfig } from 'astro/config';
import starlight from '@astrojs/starlight';

export default defineConfig({
  site: 'https://ntlx.github.io',
  base: '/ServerFS_MCP',
  trailingSlash: 'always',
  integrations: [
    starlight({
      title: 'ServerFS MCP',
      favicon: '/favicon.svg',
      logo: {
        src: './src/assets/serverfs-mark.svg',
        alt: 'ServerFS',
      },
      description:
        'Secure, scoped Linux filesystem access for ChatGPT and AI agents — read-only by default, with controlled mutations, binary transfer, and an optional Agent Bridge.',
      social: [
        {
          icon: 'github',
          label: 'GitHub',
          href: 'https://github.com/NTLx/ServerFS_MCP',
        },
      ],
      customCss: ['./src/styles/starlight.css'],
      disable404Route: true,
      sidebar: [
        {
          label: 'Start',
          items: [
            { label: 'Overview', slug: 'docs' },
            { label: 'Getting Started', slug: 'docs/getting-started' },
            { label: 'Configuration', slug: 'docs/configuration' },
          ],
        },
        {
          label: 'Capabilities',
          items: [
            { label: 'Binary Transfer', slug: 'docs/binary-transfer' },
            { label: 'Agent Bridge', slug: 'docs/agent-bridge' },
          ],
        },
        {
          label: 'Trust Boundary',
          items: [
            { label: 'Architecture', slug: 'docs/architecture' },
            { label: 'Security Model', slug: 'docs/security' },
          ],
        },
      ],
    }),
  ],
});
