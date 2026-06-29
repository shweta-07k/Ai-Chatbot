import React, { useState } from "react";

const CopyIcon = () => (
  <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden="true">
    <rect x="9" y="9" width="13" height="13" rx="2" />
    <path d="M5 15H4a2 2 0 01-2-2V4a2 2 0 012-2h9a2 2 0 012 2v1" />
  </svg>
);

const CheckIcon = () => (
  <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden="true">
    <path d="M5 12l4 4L19 6" />
  </svg>
);

export async function copyText(text) {
  if (!text) return false;
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    const area = document.createElement("textarea");
    area.value = text;
    document.body.appendChild(area);
    area.select();
    const ok = document.execCommand("copy");
    document.body.removeChild(area);
    return ok;
  }
}

export default function CopyButton({ text, label = "Copy", className = "", title = "Copy" }) {
  const [copied, setCopied] = useState(false);

  const handleCopy = async (e) => {
    e.preventDefault();
    e.stopPropagation();
    const ok = await copyText(text);
    if (ok) {
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1800);
    }
  };

  return (
    <button
      type="button"
      className={`copy-btn ${className}`.trim()}
      onClick={handleCopy}
      title={copied ? "Copied!" : title}
      aria-label={copied ? "Copied" : title}
    >
      {copied ? <CheckIcon /> : <CopyIcon />}
      {label ? <span>{copied ? "Copied" : label}</span> : null}
    </button>
  );
}

export function ShareButton({ text, className = "" }) {
  const [shared, setShared] = useState(false);

  const handleShare = async (e) => {
    e.preventDefault();
    e.stopPropagation();
    if (navigator.share) {
      try {
        await navigator.share({ title: "AI Answer", text });
        setShared(true);
        window.setTimeout(() => setShared(false), 1800);
        return;
      } catch (err) {
        if (err?.name === "AbortError") return;
      }
    }
    const ok = await copyText(text);
    if (ok) {
      setShared(true);
      window.setTimeout(() => setShared(false), 1800);
    }
  };

  return (
    <button type="button" className={`share-btn ${className}`.trim()} onClick={handleShare} title="Share answer">
      <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden="true">
        <circle cx="18" cy="5" r="3" />
        <circle cx="6" cy="12" r="3" />
        <circle cx="18" cy="19" r="3" />
        <line x1="8.59" y1="13.51" x2="15.42" y2="17.49" />
        <line x1="15.41" y1="6.51" x2="8.59" y2="10.49" />
      </svg>
      <span>{shared ? "Copied" : "Share"}</span>
    </button>
  );
}
