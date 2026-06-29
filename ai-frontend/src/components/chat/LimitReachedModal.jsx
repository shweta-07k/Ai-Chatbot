import React from "react";
import Modal from "../ui/Modal";

export default function LimitReachedModal({ open, onClose, onSignIn, onSignUp, limit = 3 }) {
  return (
    <Modal open={open} onClose={onClose} className="status-modal limit-modal" labelledBy="limit-modal-title">
      <button type="button" className="modal-close" onClick={onClose} aria-label="Close">×</button>
      <div className="status-icon status-icon-info">!</div>
      <h2 id="limit-modal-title">Your free limit has been reached</h2>
      <p>
        You&apos;ve used all {limit} free messages. Please sign in or create an account here to keep chatting,
        upload files, and save your history.
      </p>
      <div className="modal-actions limit-modal-actions">
        <button type="button" className="btn btn-secondary" onClick={onSignIn}>
          Sign in
        </button>
        <button type="button" className="btn btn-primary" onClick={onSignUp}>
          Sign up
        </button>
      </div>
    </Modal>
  );
}
