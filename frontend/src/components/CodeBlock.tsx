"use client";

import { useState, useCallback } from "react";
import { PrismLight as SyntaxHighlighter } from "react-syntax-highlighter";
import { vscDarkPlus } from "react-syntax-highlighter/dist/cjs/styles/prism";
import { Copy, Check } from "lucide-react";
import { cn } from "@/lib/utils";

// ── Register common languages (light build — only what wiki pages use) ──────
import json from "react-syntax-highlighter/dist/cjs/languages/prism/json";
import bash from "react-syntax-highlighter/dist/cjs/languages/prism/bash";
import python from "react-syntax-highlighter/dist/cjs/languages/prism/python";
import javascript from "react-syntax-highlighter/dist/cjs/languages/prism/javascript";
import typescript from "react-syntax-highlighter/dist/cjs/languages/prism/typescript";
import sql from "react-syntax-highlighter/dist/cjs/languages/prism/sql";
import yaml from "react-syntax-highlighter/dist/cjs/languages/prism/yaml";
import markdown from "react-syntax-highlighter/dist/cjs/languages/prism/markdown";
import css from "react-syntax-highlighter/dist/cjs/languages/prism/css";
import scss from "react-syntax-highlighter/dist/cjs/languages/prism/scss";
import docker from "react-syntax-highlighter/dist/cjs/languages/prism/docker";
import nginx from "react-syntax-highlighter/dist/cjs/languages/prism/nginx";
import ini from "react-syntax-highlighter/dist/cjs/languages/prism/ini";
import toml from "react-syntax-highlighter/dist/cjs/languages/prism/toml";

SyntaxHighlighter.registerLanguage("json", json);
SyntaxHighlighter.registerLanguage("bash", bash);
SyntaxHighlighter.registerLanguage("shell", bash);
SyntaxHighlighter.registerLanguage("sh", bash);
SyntaxHighlighter.registerLanguage("python", python);
SyntaxHighlighter.registerLanguage("py", python);
SyntaxHighlighter.registerLanguage("javascript", javascript);
SyntaxHighlighter.registerLanguage("js", javascript);
SyntaxHighlighter.registerLanguage("typescript", typescript);
SyntaxHighlighter.registerLanguage("ts", typescript);
SyntaxHighlighter.registerLanguage("sql", sql);
SyntaxHighlighter.registerLanguage("yaml", yaml);
SyntaxHighlighter.registerLanguage("yml", yaml);
SyntaxHighlighter.registerLanguage("markdown", markdown);
SyntaxHighlighter.registerLanguage("md", markdown);
SyntaxHighlighter.registerLanguage("css", css);
SyntaxHighlighter.registerLanguage("scss", scss);
SyntaxHighlighter.registerLanguage("dockerfile", docker);
SyntaxHighlighter.registerLanguage("docker", docker);
SyntaxHighlighter.registerLanguage("nginx", nginx);
SyntaxHighlighter.registerLanguage("ini", ini);
SyntaxHighlighter.registerLanguage("toml", toml);

// ── Language → display name ─────────────────────────────────────────────────
const LANG_LABELS: Record<string, string> = {
  json: "JSON",
  bash: "Bash",
  shell: "Shell",
  sh: "Shell",
  python: "Python",
  py: "Python",
  javascript: "JavaScript",
  js: "JavaScript",
  typescript: "TypeScript",
  ts: "TypeScript",
  sql: "SQL",
  yaml: "YAML",
  yml: "YAML",
  markdown: "Markdown",
  md: "Markdown",
  css: "CSS",
  scss: "SCSS",
  dockerfile: "Dockerfile",
  docker: "Dockerfile",
  nginx: "Nginx",
  ini: "INI",
  toml: "TOML",
};

// ── Detect language from className ──────────────────────────────────────────
function detectLang(className?: string): { lang: string; label: string } {
  if (!className) return { lang: "text", label: "" };
  const match = className.match(/language-(\w+)/);
  if (!match) return { lang: "text", label: "" };
  const raw = match[1].toLowerCase();
  const label = LANG_LABELS[raw] || raw.toUpperCase();
  return { lang: raw, label };
}

// ── Copy button ─────────────────────────────────────────────────────────────
function CopyButton({ code }: { code: string }) {
  const [copied, setCopied] = useState(false);

  const handleCopy = useCallback(async () => {
    try {
      await navigator.clipboard.writeText(code);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      // Fallback for older browsers
      const ta = document.createElement("textarea");
      ta.value = code;
      ta.style.position = "fixed";
      ta.style.opacity = "0";
      document.body.appendChild(ta);
      ta.select();
      document.execCommand("copy");
      document.body.removeChild(ta);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    }
  }, [code]);

  return (
    <button
      onClick={handleCopy}
      className={cn(
        "flex items-center gap-1 rounded px-2 py-1 text-[11px] font-medium transition-colors",
        copied
          ? "bg-emerald-600/20 text-emerald-400"
          : "text-slate-400 hover:text-slate-200 hover:bg-white/10"
      )}
      title={copied ? "Copied!" : "Copy code"}
    >
      {copied ? (
        <>
          <Check className="h-3 w-3" />
          Copied
        </>
      ) : (
        <>
          <Copy className="h-3 w-3" />
          Copy
        </>
      )}
    </button>
  );
}

// ── CodeBlock component ─────────────────────────────────────────────────────
interface CodeBlockProps {
  children?: React.ReactNode;
  className?: string;
  inline?: boolean;
  /** Pre-parsed content passed from the pre wrapper */
  raw?: string;
  lang?: string;
}

export function CodeBlock({
  children,
  className,
  inline,
  raw,
  lang: forcedLang,
}: CodeBlockProps) {
  // Inline code — render as simple backtick span
  if (inline) {
    return (
      <code className="rounded-md bg-slate-800/10 px-1.5 py-0.5 text-[0.85em] font-mono text-rose-700 dark:bg-slate-200/10 dark:text-rose-400">
        {children}
      </code>
    );
  }

  const code = raw ?? extractTextContent(children);
  const { lang, label } = forcedLang
    ? { lang: forcedLang, label: LANG_LABELS[forcedLang] || forcedLang.toUpperCase() }
    : detectLang(className);

  const showHeader = label !== "";

  return (
    <div className="my-5 overflow-hidden rounded-xl border border-slate-700/50 bg-[#1e1e1e] shadow-md">
      {/* Header bar */}
      {showHeader && (
        <div className="flex items-center justify-between border-b border-slate-700/50 bg-[#1e1e1e] px-4 py-2">
          <span className="text-[11px] font-semibold uppercase tracking-wide text-slate-400">
            {label}
          </span>
          <CopyButton code={code} />
        </div>
      )}

      {/* Code */}
      <div className="overflow-x-auto">
        <SyntaxHighlighter
          language={lang !== "text" ? lang : undefined}
          style={vscDarkPlus}
          customStyle={{
            margin: 0,
            padding: showHeader ? "1rem 1rem" : "1rem 1rem",
            background: "transparent",
            fontSize: "0.8rem",
            lineHeight: "1.6",
            borderRadius: 0,
          }}
          codeTagProps={{
            style: {
              fontFamily: "'JetBrains Mono', 'Fira Code', 'Cascadia Code', 'Consolas', 'Monaco', monospace",
            },
          }}
          wrapLines={false}
          wrapLongLines={false}
        >
          {code}
        </SyntaxHighlighter>
      </div>
    </div>
  );
}

// ── Helper: extract text from React children ────────────────────────────────
function extractTextContent(children: React.ReactNode): string {
  if (typeof children === "string") return children;
  if (Array.isArray(children)) return children.map(extractTextContent).join("");
  if (children && typeof children === "object" && "props" in children) {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    return extractTextContent((children as any).props.children);
  }
  return "";
}
