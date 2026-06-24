/** @type {import('next').NextConfig} */
const nextConfig = {
  experimental: {
    serverActions: {
      bodySizeLimit: "10mb",
    },
  },
  async rewrites() {
    return [
      // LLM may generate /output/... instead of /api/local/downloads/output/...
      // Preserve the "output" directory prefix so the download API resolves correctly.
      {
        source: "/output/:path*",
        destination: "/api/local/downloads/output/:path*",
      },
    ];
  },
};

export default nextConfig;
