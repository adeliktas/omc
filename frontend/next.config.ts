import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // In dev, Next may proxy requests based on the request origin/host.
  // Allow common local origins so `next dev --hostname 127.0.0.1` works
  // when users access via http://localhost:3000 or http://127.0.0.1:3000.
  // Add LAN origins for dev on the local network via NEXT_DEV_ORIGINS (comma-separated).
  allowedDevOrigins: [
    "localhost",
    "127.0.0.1",
    ...(process.env.NEXT_DEV_ORIGINS?.split(",").filter(Boolean) ?? []),
  ],
  images: {
    remotePatterns: [
      {
        protocol: "https",
        hostname: "img.clerk.com",
      },
    ],
  },
};

export default nextConfig;
