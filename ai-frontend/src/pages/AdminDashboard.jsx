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

function formatEventLabel(event) {
  const labels = {
    register: "Sign up",
    login: "Login",
    google_register: "Google sign up",
    google_login: "Google login",
  };
  return labels[event] || event || "—";
}

const DeleteIcon = () => (
  <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden="true">
    <polyline points="3 6 5 6 21 6" />
    <path d="M19 6l-1 14a2 2 0 01-2 2H8a2 2 0 01-2-2L5 6" />
    <path d="M10 11v6M14 11v6" />
    <path d="M9 6V4a1 1 0 011-1h4a1 1 0 011 1v2" />
  </svg>
);

const PERIOD_OPTIONS = [
  { value: "", label: "All time" },
  { value: "today", label: "Today" },
  { value: "yesterday", label: "Yesterday" },
  { value: "7days", label: "Last 7 days" },
];

export default function AdminDashboard() {
  const { user, isAuthenticated } = useAuth();
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [overview, setOverview] = useState(null);
  const [users, setUsers] = useState([]);
  const [logins, setLogins] = useState([]);
  const [chats, setChats] = useState([]);
  const [tab, setTab] = useState("users");
  const [searchEmail, setSearchEmail] = useState("");
  const [debouncedSearch, setDebouncedSearch] = useState("");
  const [period, setPeriod] = useState("");
  const [deletingEmail, setDeletingEmail] = useState("");

  useEffect(() => {
    const timer = setTimeout(() => setDebouncedSearch(searchEmail.trim()), 300);
    return () => clearTimeout(timer);
  }, [searchEmail]);

  const buildQuery = useCallback((emailQuery, extra = "") => {
    const params = new URLSearchParams();
    if (emailQuery) params.set("email", emailQuery);
    if (period) params.set("period", period);
    if (extra) {
      extra.split("&").forEach((pair) => {
        const [key, val] = pair.split("=");
        if (key && val) params.set(key, val);
      });
    }
    const qs = params.toString();
    return qs ? `?${qs}` : "";
  }, [period]);

  const loadAdminData = useCallback(async (emailQuery = "") => {
    const token = getAuthToken();
    const userQuery = buildQuery(emailQuery);
    const chatQuery = buildQuery(emailQuery, "limit=200");
    const loginQuery = buildQuery(emailQuery, "limit=300");

    setLoading(true);
    setError("");
    try {
      const [stats, userRows, loginRows, chatRows] = await Promise.all([
        apiRequest("/admin/overview", { token }),
        apiRequest(`/admin/users${userQuery}`, { token }),
        apiRequest(`/admin/logins${loginQuery}`, { token }),
        apiRequest(`/admin/chats${chatQuery}`, { token }),
      ]);
      setOverview(stats);
      setUsers(Array.isArray(userRows?.users) ? userRows.users : []);
      setLogins(Array.isArray(loginRows?.logins) ? loginRows.logins : []);
      setChats(Array.isArray(chatRows?.chats) ? chatRows.chats : []);
    } catch (err) {
      setError(friendlyChatError(err));
    } finally {
      setLoading(false);
    }
  }, [buildQuery]);

  useEffect(() => {
    if (!isAuthenticated || !user?.is_admin) return;
    loadAdminData(debouncedSearch);
  }, [isAuthenticated, user?.is_admin, debouncedSearch, period, loadAdminData]);

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

  const periodLabel = PERIOD_OPTIONS.find((p) => p.value === period)?.label || "All time";

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
        <div className="admin-search-row">
          <div style={{ flex: 1 }}>
            <label htmlFor="admin-user-search">Search by email</label>
            <input
              id="admin-user-search"
              type="search"
              className="admin-search-input"
              placeholder="e.g. user@gmail.com"
              value={searchEmail}
              onChange={(e) => setSearchEmail(e.target.value)}
              autoComplete="off"
            />
          </div>
          <div>
            <label htmlFor="admin-period">Time period</label>
            <select
              id="admin-period"
              className="admin-search-input"
              value={period}
              onChange={(e) => setPeriod(e.target.value)}
            >
              {PERIOD_OPTIONS.map((opt) => (
                <option key={opt.value || "all"} value={opt.value}>{opt.label}</option>
              ))}
            </select>
          </div>
        </div>
        {debouncedSearch ? (
          <p className="admin-search-hint">
            Showing <strong>{periodLabel}</strong> results for <strong>{debouncedSearch}</strong>
          </p>
        ) : (
          <p className="admin-search-hint">
            Showing <strong>{periodLabel}</strong> — use the <strong>Logins</strong> tab for full sign-in history.
          </p>
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
              <span>Sign-ups yesterday</span>
              <strong>{overview.registrations_yesterday ?? 0}</strong>
            </article>
            <article className="admin-stat-card">
              <span>Logins yesterday</span>
              <strong>{overview.logins_yesterday ?? 0}</strong>
            </article>
            <article className="admin-stat-card">
              <span>Logins today</span>
              <strong>{overview.logins_today ?? 0}</strong>
            </article>
            <article className="admin-stat-card">
              <span>Total chats</span>
              <strong>{overview.total_chats}</strong>
            </article>
            <article className="admin-stat-card">
              <span>Google users</span>
              <strong>{overview.google_users}</strong>
            </article>
          </section>

          <div className="admin-tabs">
            <button type="button" className={tab === "users" ? "active" : ""} onClick={() => setTab("users")}>
              Users ({users.length})
            </button>
            <button type="button" className={tab === "logins" ? "active" : ""} onClick={() => setTab("logins")}>
              Logins ({logins.length})
            </button>
            <button type="button" className={tab === "chats" ? "active" : ""} onClick={() => setTab("chats")}>
              Chats ({chats.length})
            </button>
          </div>

          {tab === "users" && (
            <section className="admin-panel">
              {users.length === 0 ? (
                <p className="admin-empty">No users match this filter.</p>
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
                        <th>Last login</th>
                        <th>Logins</th>
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
                          <td>{formatWhen(row.last_login_at)}</td>
                          <td>{row.login_count ?? 0}</td>
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

          {tab === "logins" && (
            <section className="admin-panel">
              {logins.length === 0 ? (
                <p className="admin-empty">
                  No login events for this filter. New logins are recorded after deploy — ask users to sign in again.
                </p>
              ) : (
                <div className="admin-table-wrap">
                  <table className="admin-table">
                    <thead>
                      <tr>
                        <th>When</th>
                        <th>Email</th>
                        <th>Username</th>
                        <th>Event</th>
                        <th>Provider</th>
                      </tr>
                    </thead>
                    <tbody>
                      {logins.map((row) => (
                        <tr key={row._id}>
                          <td>{formatWhen(row.created_at)}</td>
                          <td>{row.email}</td>
                          <td>{row.username || "—"}</td>
                          <td>{formatEventLabel(row.event)}</td>
                          <td>{row.auth_provider || "—"}</td>
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
