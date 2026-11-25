import type { NextConfig } from 'next';
import { SECURITY_HEADERS } from './src/lib/securityHeaders';

const nextConfig: NextConfig = {
  reactStrictMode: true,
  output: 'standalone',
  poweredByHeader: false,
  // /licences reads the notices file at request time (src/lib/notices.ts): it must be in the
  // standalone output the image runs.
  outputFileTracingIncludes: { '/licences': ['./src/legal/THIRD_PARTY_NOTICES.txt'] },
  // TypeScript is a build tool. The standalone server runs the compiled output with the config
  // already embedded, so the compiler (and its advisories) stays out of the runtime image.
  outputFileTracingExcludes: { '*': ['node_modules/typescript/**'] },
  async headers() {
    return [{ source: '/:path*', headers: SECURITY_HEADERS }];
  },
};

export default nextConfig;
