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
  // The container runs the dev server against a bind mount; standalone output
  // keeps a production image small when one is built.
  output: process.env.NEXT_OUTPUT_STANDALONE === 'true' ? 'standalone' : undefined,
  eslint: {
    // Lint is a separate CI step; a lint error should not fail a build.
    ignoreDuringBuilds: true,
  },
};

module.exports = nextConfig;
