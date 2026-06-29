import React from "react";

export default function AiLoadingBubble({ label = "AI is thinking" }) {
  return (
    <div className="ai-loading-bubble" role="status" aria-live="polite">
      <div className="typing-indicator" aria-hidden="true">
        <span />
        <span />
        <span />
      </div>
      <div className="ai-loading-copy">
        <strong>{label}</strong>
        <span>Analyzing your question…</span>
      </div>
    </div>
  );
}
