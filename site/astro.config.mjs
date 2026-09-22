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
        'Secure, scoped Linux filesystem access for ChatGPT and AI agents — read-only by default, with controlled mutations, binary transfer, an optional Agent Bridge, and opt-in Jev advisory decisions.',
      defaultLocale: 'root',
      locales: {
        root: {
          label: 'English',
          lang: 'en',
        },
        'zh-cn': {
          label: '简体中文',
          lang: 'zh-CN',
        },
      },
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
          translations: { 'zh-CN': '开始' },
          items: [
            { label: 'Overview', translations: { 'zh-CN': '概览' }, slug: 'docs' },
            {
              label: 'Getting Started',
              translations: { 'zh-CN': '快速开始' },
              slug: 'docs/getting-started',
            },
            {
              label: 'Configuration',
              translations: { 'zh-CN': '配置' },
              slug: 'docs/configuration',
            },
          ],
        },
        {
          label: 'Capabilities',
          translations: { 'zh-CN': '能力' },
          items: [
            {
              label: 'Binary Transfer',
              translations: { 'zh-CN': '二进制传输' },
              slug: 'docs/binary-transfer',
            },
            {
              label: 'Agent Bridge',
              translations: { 'zh-CN': 'Agent Bridge' },
              slug: 'docs/agent-bridge',
            },
            {
              label: 'Jev Advisors',
              translations: { 'zh-CN': 'Jev Advisors' },
              slug: 'docs/jev-advisors',
            },
          ],
        },
        {
          label: 'Trust Boundary',
          translations: { 'zh-CN': '信任边界' },
          items: [
            {
              label: 'Architecture',
              translations: { 'zh-CN': '架构' },
              slug: 'docs/architecture',
            },
            {
              label: 'Security Model',
              translations: { 'zh-CN': '安全模型' },
              slug: 'docs/security',
            },
          ],
        },
      ],
    }),
  ],
});
