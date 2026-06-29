import React from "react";
import Modal from "./Modal";

export default function StatusModal({
  open,
  onClose,
  type = "info",
  title,
  message,
  primaryLabel = "Got it",
  onPrimary,
  secondaryLabel,
  onSecondary,
}) {
  const icons = { success: "✓", error: "!", info: "i", logout: "↪" };

  return (
    <Modal open={open} onClose={onClose} className="status-modal" labelledBy="status-modal-title">
      <button type="button" className="modal-close" onClick={onClose} aria-label="Close">×</button>
      <div className={`status-icon status-icon-${type}`}>{icons[type] || icons.info}</div>
      <h2 id="status-modal-title">{title}</h2>
      <p>{message}</p>
      <div className="modal-actions">
        {secondaryLabel && (
          <button type="button" className="btn btn-secondary" onClick={onSecondary || onClose}>
            {secondaryLabel}
          </button>
        )}
        <button type="button" className="btn btn-primary" onClick={onPrimary || onClose}>
          {primaryLabel}
        </button>
      </div>
    </Modal>
  );
}
