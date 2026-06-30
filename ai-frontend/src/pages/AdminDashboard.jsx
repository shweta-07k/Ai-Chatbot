import React, { useCallback, useEffect, useState } from "react";
import { Link, Navigate } from "react-router-dom";
import { useAuth } from "../context/AuthContext";
import { apiRequest, getAuthToken } from "../api/client";
import { friendlyChatError } from "../utils/friendlyErrors";

function formatWhen(value) {
  if (!value) return "—";
  try {
    return new Date(value).toLocaleString();
  } catch {
    return "—";
  }
}

const DeleteIcon = () => (
  <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden="true">
    <polyline points="3 6 5 6 21 6" />
    <path d="M19 6l-1 14a2 2 0 01-2 2H8a2 2 0 01-2-2L5 6" />
    <path d="M10 11v6M14 11v6" />
    <path d="M9 6V4a1 1 0 011-1h4a1 1 0 011 1v2" />
  </svg>
);

export default function AdminDashboard() {
  const { user, isAuthenticated } = useAuth();
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [overview, setOverview] = useState(null);
  const [users, setUsers] = useState([]);
  const [chats, setChats] = useState([]);
  const [tab, setTab] = useState("users");
  const [searchEmail, setSearchEmail] = useState("");
  const [debouncedSearch, setDebouncedSearch] = useState("");
  const [deletingEmail, setDeletingEmail] = useState("");

  useEffect(() => {
    const timer = setTimeout(() => setDebouncedSearch(searchEmail.trim()), 300);
    return () => clearTimeout(timer);
  }, [searchEmail]);

  const loadAdminData = useCallback(async (emailQuery = "") => {
    const token = getAuthToken();
    const query = emailQuery ? `?email=${encodeURIComponent(emailQuery)}` : "";
    const chatQuery = emailQuery
      ? `?limit=200&email=${encodeURIComponent(emailQuery)}`
      : "?limit=100";

    setLoading(true);
    setError("");
    try {
      const [stats, userRows, chatRows] = await Promise.all([
        apiRequest("/admin/overview", { token }),
        apiRequest(`/admin/users${query}`, { token }),
        apiRequest(`/admin/chats${chatQuery}`, { token }),
      ]);
      setOverview(stats);
      setUsers(Array.isArray(userRows?.users) ? userRows.users : []);
      setChats(Array.isArray(chatRows?.chats) ? chatRows.chats : []);
    } catch (err) {
      setError(friendlyChatError(err));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (!isAuthenticated || !user?.is_admin) return;
    loadAdminData(debouncedSearch);
  }, [isAuthenticated, user?.is_admin, debouncedSearch, loadAdminData]);

  const handleDeleteUser = async (row) => {
    const targetEmail = row.email;
    if (!targetEmail) return;

    const confirmed = window.confirm(
      `Delete user "${row.username || targetEmail}" (${targetEmail})?\n\nThis will permanently remove their account and all chat history.`
    );
    if (!confirmed) return;

    setDeletingEmail(targetEmail);
    setError("");
    try {
      const token = getAuthToken();
      await apiRequest(`/admin/users?email=${encodeURIComponent(targetEmail)}`, {
        method: "DELETE",
        token,
      });
      await loadAdminData(debouncedSearch);
    } catch (err) {
      setError(friendlyChatError(err));
    } finally {
      setDeletingEmail("");
    }
  };

  const canDeleteUser = (row) => {
    const email = (row.email || "").toLowerCase();
    const self = (user?.email || "").toLowerCase();
    return email && email !== self && !row.is_admin;
  };

  if (!isAuthenticated) {
    return <Navigate to="/?auth=login" replace />;
  }

  if (!user?.is_admin) {
    return <Navigate to="/" replace />;
  }

  return (
    <div className="admin-shell">
      <header className="admin-topbar">
        <div>
          <h1>Admin Dashboard</h1>
          <p>Signed in as {user.email}</p>
        </div>
        <Link to="/" className="btn btn-secondary">Back to chat</Link>
      </header>

      <div className="admin-search-bar">
        <label htmlFor="admin-user-search">Search by email</label>
        <div className="admin-search-row">
          <input
            id="admin-user-search"
            type="search"
            className="admin-search-input"
            placeholder="e.g. user@gmail.com"
            value={searchEmail}
            onChange={(e) => setSearchEmail(e.target.value)}
            autoComplete="off"
          />
          {searchEmail ? (
            <button type="button" className="btn btn-ghost btn-sm" onClick={() => setSearchEmail("")}>
              Clear
            </button>
          ) : null}
        </div>
        {debouncedSearch ? (
          <p className="admin-search-hint">
            Showing results for <strong>{debouncedSearch}</strong>
            {users.length === 1 ? " — switch to Chats tab to see this user's messages." : ""}
          </p>
        ) : (
          <p className="admin-search-hint">Type an email to filter users and their chat history.</p>
        )}
      </div>

      {loading && <div className="admin-panel">Loading admin data...</div>}
      {error && <div className="admin-panel admin-error">{error}</div>}

      {!loading && !error && overview && (
        <>
          <section className="admin-stats">
            <article className="admin-stat-card">
              <span>Total users</span>
              <strong>{overview.total_users}</strong>
            </article>
            <article className="admin-stat-card">
              <span>Total chats</span>
              <strong>{overview.total_chats}</strong>
            </article>
            <article className="admin-stat-card">
              <span>Google sign-ins</span>
              <strong>{overview.google_users}</strong>
            </article>
            <article className="admin-stat-card">
              <span>Email sign-ups</span>
              <strong>{overview.email_users}</strong>
            </article>
          </section>

          <div className="admin-tabs">
            <button type="button" className={tab === "users" ? "active" : ""} onClick={() => setTab("users")}>
              Users ({users.length})
            </button>
            <button type="button" className={tab === "chats" ? "active" : ""} onClick={() => setTab("chats")}>
              Chats ({chats.length})
            </button>
          </div>

          {tab === "users" && (
            <section className="admin-panel">
              {users.length === 0 ? (
                <p className="admin-empty">No users match this email search.</p>
              ) : (
                <div className="admin-table-wrap">
                  <table className="admin-table">
                    <thead>
                      <tr>
                        <th>Username</th>
                        <th>Email</th>
                        <th>Provider</th>
                        <th>Chats</th>
                        <th>Joined</th>
                        <th aria-label="Actions" />
                      </tr>
                    </thead>
                    <tbody>
                      {users.map((row) => (
                        <tr key={row.email}>
                          <td>{row.username || "—"}</td>
                          <td>{row.email}</td>
                          <td>{row.auth_provider || "email"}</td>
                          <td>{row.chat_count ?? 0}</td>
                          <td>{formatWhen(row.created_at)}</td>
                          <td className="admin-actions-cell">
                            {canDeleteUser(row) ? (
                              <button
                                type="button"
                                className="admin-delete-btn"
                                title={`Delete ${row.email}`}
                                aria-label={`Delete ${row.email}`}
                                disabled={deletingEmail === row.email}
                                onClick={() => handleDeleteUser(row)}
                              >
                                <DeleteIcon />
                              </button>
                            ) : (
                              <span className="admin-protected" title="Protected account">—</span>
                            )}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </section>
          )}

          {tab === "chats" && (
            <section className="admin-panel admin-chat-list">
              {chats.length === 0 ? (
                <p className="admin-empty">No chats match this search.</p>
              ) : (
                chats.map((chat) => (
                  <article className="admin-chat-card" key={chat._id}>
                    <header>
                      <strong>{chat.user_email || "guest"}</strong>
                      <span>{formatWhen(chat.timestamp)}</span>
                    </header>
                    <p className="admin-chat-label">Question</p>
                    <p className="admin-chat-text">{chat.user_query}</p>
                    <p className="admin-chat-label">Answer</p>
                    <p className="admin-chat-text admin-chat-answer">{chat.ai_response}</p>
                    {chat.session_id ? <small>Session: {chat.session_id}</small> : null}
                  </article>
                ))
              )}
            </section>
          )}
        </>
      )}
    </div>
  );
}
