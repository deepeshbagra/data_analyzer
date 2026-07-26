import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  reactStrictMode: true,
  // The API is a separate service; the browser talks to it directly using a
  // signed, expiring URL for document access (see Phase 4).
  env: {
    NEXT_PUBLIC_API_BASE_URL: process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000",
  },
};

export default nextConfig;
