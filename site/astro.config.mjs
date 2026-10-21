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
        'Secure, standards-first MCP filesystem access on Linux, Windows and macOS — read-only by default, with Streamable HTTP, optional API Key or OAuth 2.1 authentication, controlled mutations, binary transfer and Agent delegation.',
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
            {
              label: 'Static API Key',
              translations: { 'zh-CN': '静态 API Key' },
              slug: 'docs/api-key',
            },
            {
              label: 'OAuth 2.1',
              translations: { 'zh-CN': 'OAuth 2.1' },
              slug: 'docs/oauth',
            },
            {
              label: 'Windows Native',
              translations: { 'zh-CN': 'Windows 原生部署' },
              slug: 'docs/windows-native',
            },
            {
              label: 'macOS Native',
              translations: { 'zh-CN': 'macOS 原生部署' },
              slug: 'docs/macos-native',
            },
          ],
        },
        {
          label: 'Capabilities',
          translations: { 'zh-CN': '能力' },
          items: [
            {
              label: 'Agent Skills',
              translations: { 'zh-CN': 'Agent Skills' },
              slug: 'docs/agent-skills',
            },
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
            { label: 'ACP Guide', translations: { 'zh-CN': 'ACP 指南' }, slug: 'docs/acp' },
            {
              label: 'Jev Advisors',
              translations: { 'zh-CN': 'Jev Advisors' },
              slug: 'docs/jev-advisors',
            },
          ],
        },
        {
          label: 'Operations',
          translations: { 'zh-CN': '运维' },
          items: [
            {
              label: 'Tunnel Diagnostics',
              translations: { 'zh-CN': 'Tunnel 诊断' },
              slug: 'docs/tunnel-diagnostics',
            },
            {
              label: 'Host Observability',
              translations: { 'zh-CN': '宿主机可观测性' },
              slug: 'docs/host-observability',
            },
            {
              label: 'Restricted-network ARM64',
              translations: { 'zh-CN': '受限网络 ARM64' },
              slug: 'docs/restricted-network-arm64',
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
