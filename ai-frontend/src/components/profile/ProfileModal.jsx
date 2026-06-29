import React, { useEffect, useState } from "react";
import Modal from "../ui/Modal";
import { useAuth } from "../../context/AuthContext";
import { friendlyError } from "../../utils/friendlyErrors";

export default function ProfileModal({ open, onClose, onSaved }) {
  const { user, updateProfile, refreshProfile } = useAuth();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [success, setSuccess] = useState("");

  useEffect(() => {
    if (!open) return;
    setUsername(user?.username || "");
    setPassword("");
    setConfirmPassword("");
    setError("");
    setSuccess("");
    refreshProfile().catch(() => {});
  }, [open, user?.username, refreshProfile]);

  const handleSubmit = async (e) => {
    e.preventDefault();
    setError("");
    setSuccess("");

    if (!username.trim() || username.trim().length < 2) {
      setError("Display name must be at least 2 characters.");
      return;
    }

    const payload = { username: username.trim() };
    if (password) {
      if (password.length < 8) {
        setError("New password must be at least 8 characters.");
        return;
      }
      if (password !== confirmPassword) {
        setError("Passwords do not match.");
        return;
      }
      payload.password = password;
    }

    setLoading(true);
    try {
      await updateProfile(payload);
      setSuccess("Profile updated successfully.");
      onSaved?.();
      setPassword("");
      setConfirmPassword("");
    } catch (err) {
      setError(friendlyError(err.message));
    } finally {
      setLoading(false);
    }
  };

  return (
    <Modal open={open} onClose={onClose} className="profile-modal" labelledBy="profile-modal-title">
      <button type="button" className="modal-close" onClick={onClose} aria-label="Close">×</button>

      <div className="profile-header">
        <div className="profile-avatar">
          {user?.avatar_url ? (
            <img src={user.avatar_url} alt="" />
          ) : (
            (user?.username || "U").charAt(0).toUpperCase()
          )}
        </div>
        <div>
          <h2 id="profile-modal-title">Edit profile</h2>
          <p>{user?.email}</p>
        </div>
      </div>

      <form className="auth-form" onSubmit={handleSubmit}>
        <label>
          Display name
          <input type="text" value={username} onChange={(e) => setUsername(e.target.value)} placeholder="Your name" />
        </label>
        <label>
          New password <span className="label-muted">(optional)</span>
          <input type="password" value={password} onChange={(e) => setPassword(e.target.value)} placeholder="Leave blank to keep current" autoComplete="new-password" />
        </label>
        {password && (
          <label>
            Confirm new password
            <input type="password" value={confirmPassword} onChange={(e) => setConfirmPassword(e.target.value)} placeholder="Repeat password" autoComplete="new-password" />
          </label>
        )}
        {user?.auth_provider === "google" && !password && (
          <p className="auth-note">Signed in with Google. Add a password to also use email login.</p>
        )}
        {error && <div className="form-error" role="alert">{error}</div>}
        {success && <div className="form-success" role="status">{success}</div>}
        <div className="modal-actions">
          <button type="button" className="btn btn-secondary" onClick={onClose}>Cancel</button>
          <button type="submit" className="btn btn-primary" disabled={loading}>
            {loading ? "Saving..." : "Save changes"}
          </button>
        </div>
      </form>
    </Modal>
  );
}
