import type { NextConfig } from 'next';
import { SECURITY_HEADERS } from './src/lib/securityHeaders';

const nextConfig: NextConfig = {
  reactStrictMode: true,
  output: 'standalone',
  poweredByHeader: false,
  async headers() {
    return [{ source: '/:path*', headers: SECURITY_HEADERS }];
  },
};

export default nextConfig;
