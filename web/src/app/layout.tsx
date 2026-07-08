import type { Metadata } from "next";
import "./globals.css";
import localFont from "next/font/local";
import React from "react";
import { NuqsAdapter } from "nuqs/adapters/next/app";

const geist = localFont({
  src: "./fonts/Geist-Variable.woff2",
  weight: "100 900",
  style: "normal",
  preload: true,
  display: "swap",
  fallback: [
    "Segoe UI",
    "Microsoft YaHei",
    "PingFang SC",
    "Helvetica Neue",
    "Arial",
    "sans-serif",
  ],
});

export const metadata: Metadata = {
  title: "qingzhou-agent",
  description: "qingzhou-agent",
  icons: {
    icon: "/favicon.ico",
    shortcut: "/favicon.ico",
    apple: "/qingzhou-logo.png",
  },
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en">
      <body className={geist.className}>
        <NuqsAdapter>{children}</NuqsAdapter>
      </body>
    </html>
  );
}
