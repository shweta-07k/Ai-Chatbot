import React from "react";
import Modal from "../ui/Modal";
import MessageMarkdown, { formatTimestamp } from "./MessageMarkdown";

export default function HistoryModal({
  open,
  onClose,
  logs,
  selected,
  onSelect,
  onClearSelection,
  onDelete,
}) {
  return (
    <>
      <Modal open={open} onClose={onClose} className="panel-modal memory-modal history-modal" labelledBy="history-title">
        <button type="button" className="modal-close" onClick={onClose} aria-label="Close">×</button>

        <div className="panel-header panel-header-rich">
          <div>
            <h2 id="history-title">Chat History</h2>
            <p>Saved questions and answers from your account.</p>
          </div>
          <span className="panel-badge">{logs.length} saved</span>
        </div>

        <div className="history-grid memory-grid">
          {logs.length ? logs.map((log) => (
            <article
              key={log._id}
              className="memory-card history-card"
              onClick={() => onSelect(log)}
              role="button"
              tabIndex={0}
              onKeyDown={(e) => e.key === "Enter" && onSelect(log)}
            >
              <div className="memory-card-top">
                <span className="memory-tag">Question</span>
                <button
                  type="button"
                  className="delete-btn"
                  onClick={(e) => { e.stopPropagation(); onDelete(log._id); }}
                  aria-label="Delete conversation"
                  title="Delete"
                >
                  <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden="true">
                    <polyline points="3 6 5 6 21 6" />
                    <path d="M19 6v14a2 2 0 01-2 2H7a2 2 0 01-2-2V6m3 0V4a2 2 0 012-2h4a2 2 0 012 2v2" />
                    <line x1="10" y1="11" x2="10" y2="17" />
                    <line x1="14" y1="11" x2="14" y2="17" />
                  </svg>
                </button>
              </div>
              <h4>{log.user_query}</h4>
              <p className="memory-preview">{(log.ai_response || "").slice(0, 140)}{(log.ai_response || "").length > 140 ? "…" : ""}</p>
              <footer className="memory-meta">
                <span>{formatTimestamp(log.timestamp || log.created_at) || "Recently saved"}</span>
                <span className="memory-action">View</span>
              </footer>
            </article>
          )) : (
            <div className="empty-panel">
              <h3>No history yet</h3>
              <p>Chat while signed in and your conversations will appear here.</p>
            </div>
          )}
        </div>
      </Modal>

      <Modal open={Boolean(selected)} onClose={onClearSelection} className="panel-modal detail-modal" labelledBy="history-detail-title">
        <button type="button" className="modal-close" onClick={onClearSelection} aria-label="Close">×</button>
        <div className="panel-header panel-header-rich">
          <div>
            <h2 id="history-detail-title">Conversation</h2>
            <p>{formatTimestamp(selected?.timestamp || selected?.created_at) || "Saved chat"}</p>
          </div>
        </div>
        {selected && (
          <div className="detail-sections">
            <section className="detail-block">
              <h3>Your question</h3>
              <p>{selected.user_query}</p>
            </section>
            <section className="detail-block answer-block">
              <h3>Answer</h3>
              <MessageMarkdown text={selected.ai_response} role="ai" />
            </section>
          </div>
        )}
      </Modal>
    </>
  );
}
