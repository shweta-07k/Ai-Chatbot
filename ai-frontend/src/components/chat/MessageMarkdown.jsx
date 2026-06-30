import React from "react";
import ReactMarkdown from "react-markdown";
import CopyButton, { ShareButton } from "./CopyButton";

const EditIcon = () => (
  <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden="true">
    <path d="M12 20h9" />
    <path d="M16.5 3.5a2.12 2.12 0 013 3L7 19l-4 1 1-4 12.5-12.5z" />
  </svg>
);

function formatTimestamp(value) {
  if (!value) return "";
  try {
    return new Date(value).toLocaleString(undefined, {
      dateStyle: "medium",
      timeStyle: "short",
    });
  } catch {
    return "";
  }
}

function extractText(children) {
  if (children == null) return "";
  if (typeof children === "string") return children;
  if (Array.isArray(children)) return children.map(extractText).join("");
  if (typeof children === "object" && children.props) return extractText(children.props.children);
  return String(children);
}

function InlineCodeWithCopy({ children, ...props }) {
  const codeText = extractText(children).replace(/\n$/, "");
  return (
    <span className="inline-code-wrap">
      <code className="inline-code" {...props}>{children}</code>
      <CopyButton text={codeText} label="" className="copy-btn-inline" title="Copy command" />
    </span>
  );
}

function CodeBlockWithCopy({ children, className, ...props }) {
  const codeText = extractText(children).replace(/\n$/, "");
  return (
    <div className="code-block-wrap">
      <div className="code-block-header">
        <span className="code-block-label">Command</span>
        <CopyButton text={codeText} label="Copy" className="copy-btn-block" title="Copy code block" />
      </div>
      <pre className="code-block">
        <code className={className} {...props}>{children}</code>
      </pre>
    </div>
  );
}

export default function MessageMarkdown({
  text,
  role = "ai",
  showActions = true,
  onEdit = null,
}) {
  const isAi = role === "ai";
  const canShowActions = showActions && text?.trim();

  return (
    <div className={`message-hover-wrap ${isAi ? "is-ai" : "is-user"}`}>
      <div className={`message-content ${isAi ? "message-ai-rich" : "message-user-rich"}`}>
        <div className="markdown-body gemini-style no-scroll-content">
          <ReactMarkdown
          components={{
            a: ({ children, ...props }) => (
              <a {...props} className="md-link" target="_blank" rel="noreferrer">{children}</a>
            ),
            p: ({ children }) => <p className="md-paragraph">{children}</p>,
            ul: ({ children }) => <ul className="md-list">{children}</ul>,
            ol: ({ children }) => <ol className="md-list md-list-ordered">{children}</ol>,
            li: ({ children, ordered }) => (
              <li className={ordered ? "md-oli" : "md-li"}>{children}</li>
            ),
            strong: ({ children }) => <strong className="md-strong">{children}</strong>,
            h1: ({ children }) => <h3 className="md-heading">{children}</h3>,
            h2: ({ children }) => <h3 className="md-heading">{children}</h3>,
            h3: ({ children }) => <h4 className="md-subheading">{children}</h4>,
            code: ({ inline, children, className, ...props }) =>
              inline ? (
                <InlineCodeWithCopy className={className} {...props}>{children}</InlineCodeWithCopy>
              ) : (
                <CodeBlockWithCopy className={className} {...props}>{children}</CodeBlockWithCopy>
              ),
          }}
        >
          {text}
        </ReactMarkdown>
        </div>
      </div>
      {canShowActions && (
        <div className="message-hover-actions">
          <CopyButton
            text={text}
            label="Copy"
            title={isAi ? "Copy answer" : "Copy question"}
          />
          <ShareButton
            text={text}
            title={isAi ? "Share answer" : "Share question"}
            shareTitle={isAi ? "Nova AI Answer" : "Nova AI Question"}
          />
          {!isAi && onEdit ? (
            <button type="button" className="edit-btn" onClick={onEdit} title="Edit question">
              <EditIcon />
              <span>Edit</span>
            </button>
          ) : null}
        </div>
      )}
    </div>
  );
}

export { formatTimestamp };
