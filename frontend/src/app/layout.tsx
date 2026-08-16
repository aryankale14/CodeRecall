import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "CodeRecall | AI Repository Intelligence Platform",
  description: "High-performance, deep code reasoning repository analyzer, static vulnerability finder, and semantic architecture Q&A assistant powered by Gemini 3.5.",
  keywords: ["AI Code Review", "Static Analysis", "Code Vulnerabilities", "Gemini AI", "FastAPI RAG", "Repository Explorer"],
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    // data-scroll-behavior="smooth" opts back into Next.js managing scroll
    // during navigation. As of Next.js 16 the framework no longer overrides a
    // global `scroll-behavior: smooth`, so without this a route change would
    // smooth-scroll to the top instead of jumping there.
    <html lang="en" data-scroll-behavior="smooth">
      <head>
        <meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1" />
      </head>
      <body>
        {children}
      </body>
    </html>
  );
}
