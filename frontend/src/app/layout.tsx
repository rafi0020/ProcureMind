import "@fontsource/schibsted-grotesk/400.css";
import "@fontsource/schibsted-grotesk/500.css";
import "@fontsource/schibsted-grotesk/600.css";
import "@fontsource/spline-sans-mono/400.css";
import "@fontsource/spline-sans-mono/500.css";
import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "ProcureMind AI",
  description: "Invoices read, matched and checked; exceptions investigated by an agent; approvals by amount. Nothing is paid.",
};

// Runs before first paint so a saved theme choice is applied without a flash.
const themeScript = `try{var t=localStorage.getItem("theme");if(t)document.documentElement.dataset.theme=t}catch(e){}`;

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" data-theme="light" suppressHydrationWarning>
      <head>
        <script dangerouslySetInnerHTML={{ __html: themeScript }} />
      </head>
      <body>{children}</body>
    </html>
  );
}
