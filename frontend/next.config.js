/**
 * Next.js configuration.
 *
 * `NEXT_PUBLIC_API_URL` is read directly from the environment by the client
 * code, so nothing needs to be declared here for it.
 *
 * @type {import('next').NextConfig}
 */
const nextConfig = {
  reactStrictMode: true,
  // Proxy API calls through this Next.js origin when the backend is deployed
  // separately. This keeps browser requests and refresh cookies same-origin.
  async rewrites() {
    const target = process.env.API_PROXY_TARGET?.replace(/\/+$/, '');
    if (!target) return [];

    const parsedTarget = new URL(target);
    if (
      !['http:', 'https:'].includes(parsedTarget.protocol) ||
      parsedTarget.username ||
      parsedTarget.password ||
      parsedTarget.pathname !== '/' ||
      parsedTarget.search ||
      parsedTarget.hash
    ) {
      throw new Error('API_PROXY_TARGET must be an HTTP(S) origin without a path or credentials.');
    }

    return [
      { source: '/auth/:path*', destination: `${target}/auth/:path*` },
      { source: '/v1/:path*', destination: `${target}/v1/:path*` },
      { source: '/health', destination: `${target}/health` },
    ];
  },
  // The container runs the dev server against a bind mount; standalone output
  // keeps a production image small when one is built.
  output: process.env.NEXT_OUTPUT_STANDALONE === 'true' ? 'standalone' : undefined,
  eslint: {
    // Lint is a separate CI step; a lint error should not fail a build.
    ignoreDuringBuilds: true,
  },
};

module.exports = nextConfig;
