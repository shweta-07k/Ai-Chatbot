import React from "react";
import Modal from "../ui/Modal";
import MessageMarkdown, { formatTimestamp } from "./MessageMarkdown";

export default function SearchModal({
  open,
  onClose,
  query,
  onQueryChange,
  onSearch,
  loading,
  error,
  touched,
  results,
  expandedIndex,
  onToggleExpand,
  onOpenInChat,
}) {
  return (
    <Modal open={open} onClose={onClose} className="panel-modal memory-modal" labelledBy="search-title">
      <button type="button" className="modal-close" onClick={onClose} aria-label="Close">×</button>

      <div className="panel-header panel-header-rich">
        <div>
          <h2 id="search-title">Search Memory</h2>
          <p>Find past answers by keyword or topic.</p>
        </div>
      </div>

      <div className="search-tips">
        <span>Try:</span>
        {["resume", "database", "uploaded file"].map((tip) => (
          <button key={tip} type="button" className="tip-chip" onClick={() => onQueryChange(tip)}>
            {tip}
          </button>
        ))}
      </div>

      <div className="panel-search search-bar-rich">
        <input
          value={query}
          onChange={(e) => onQueryChange(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && onSearch()}
          placeholder="Search your past conversations..."
        />
        <button type="button" className="btn btn-primary" onClick={onSearch} disabled={loading}>
          {loading ? "Searching…" : "Search"}
        </button>
      </div>

      {loading && <div className="search-status loading-status">Searching…</div>}
      {error && <div className="form-error" role="alert">{error}</div>}
      {!loading && !error && touched && results.length === 0 && (
        <div className="empty-panel compact">
          <h3>No matches</h3>
          <p>Try a different keyword or shorter phrase.</p>
        </div>
      )}

      <div className="history-grid memory-grid">
        {results.map((res, i) => (
          <article key={res._id || i} className={`memory-card search-card ${expandedIndex === i ? "expanded" : ""}`}>
            <div className="memory-card-top">
              <span className="memory-tag match-tag">Match {i + 1}</span>
            </div>
            <h4>{res.user_query}</h4>
            <div className={`memory-answer ${expandedIndex === i ? "expanded" : "clamped"}`}>
              <MessageMarkdown text={res.ai_response} role="ai" />
            </div>
            <footer className="memory-meta">
              <button type="button" className="link-btn" onClick={() => onToggleExpand(i)}>
                {expandedIndex === i ? "Show less" : "Show more"}
              </button>
              <button type="button" className="btn btn-secondary btn-sm" onClick={() => onOpenInChat(res)}>
                Open in chat
              </button>
            </footer>
            {res.timestamp && <small className="memory-date">{formatTimestamp(res.timestamp)}</small>}
          </article>
        ))}
      </div>
    </Modal>
  );
}
