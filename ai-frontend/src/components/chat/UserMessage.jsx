import React from "react";
import CopyButton from "./CopyButton";

const EditIcon = () => (
  <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden="true">
    <path d="M12 20h9" />
    <path d="M16.5 3.5a2.12 2.12 0 013 3L7 19l-4 1 1-4 12.5-12.5z" />
  </svg>
);

export default function UserMessage({ text, onEdit }) {
  const displayText = (text || "").replace(/\n\n\[Attached:[^\]]+\]\s*$/, "").trim();

  return (
    <div className="user-message-stack">
      <div className="message-bubble user-question-bubble">
        <p className="user-question-text">{displayText}</p>
      </div>
      <div className="message-hover-actions user-message-actions">
        <CopyButton text={displayText} label="Copy" title="Copy question" />
        {onEdit ? (
          <button type="button" className="edit-btn" onClick={onEdit} title="Edit question">
            <EditIcon />
            <span>Edit</span>
          </button>
        ) : null}
      </div>
    </div>
  );
}
