import React, { useEffect, useRef, useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { API_URL, INITIAL_AI_MESSAGE, GUEST_MESSAGE_LIMIT } from "../config";
import { apiRequest, getAuthToken } from "../api/client";
import { friendlyChatError, friendlyUploadError } from "../utils/friendlyErrors";
import { useAuth } from "../context/AuthContext";
import { useTheme } from "../context/ThemeContext";
import MessageMarkdown from "../components/chat/MessageMarkdown";
import AiLoadingBubble from "../components/chat/AiLoadingBubble";
import AuthModal from "../components/auth/AuthModal";
import ProfileModal from "../components/profile/ProfileModal";
import StatusModal from "../components/ui/StatusModal";
import HistoryModal from "../components/chat/HistoryModal";
import LimitReachedModal from "../components/chat/LimitReachedModal";
import SearchModal from "../components/chat/SearchModal";

function sessionKeyFor(email) {
  return email ? `chatSessionId-${email}` : "chatSessionId";
}

function readStoredMessages(sid) {
  try {
    const raw = localStorage.getItem(`chatMessages-${sid}`);
    if (!raw) return null;
    const parsed = JSON.parse(raw);
    return Array.isArray(parsed) && parsed.length ? parsed : null;
  } catch {
    return null;
  }
}

function getStoredSessionId(email) {
  const key = sessionKeyFor(email);
  const stored = localStorage.getItem(key);
  if (stored) return stored;
  const id = `session_${Date.now()}_${Math.random().toString(36).slice(2, 11)}`;
  localStorage.setItem(key, id);
  return id;
}

export default function ChatApp({ googleEnabled = false }) {
  const { user, isAuthenticated, logout, startFreshSession } = useAuth();
  const { toggleTheme, isDark } = useTheme();
  const navigate = useNavigate();
  const location = useLocation();

  const [sessionId, setSessionId] = useState(() => getStoredSessionId(localStorage.getItem("userEmail")));
  const [messages, setMessages] = useState(() => readStoredMessages(getStoredSessionId(localStorage.getItem("userEmail"))) || [INITIAL_AI_MESSAGE]);
  const [input, setInput] = useState("");
  const [selectedFiles, setSelectedFiles] = useState([]);
  const [loading, setLoading] = useState(false);
  const [uploadingFiles, setUploadingFiles] = useState(false);
  const [msgCount, setMsgCount] = useState(Number(localStorage.getItem("queryCount")) || 0);
  const [sidebarOpen, setSidebarOpen] = useState(false);

  const [authModalOpen, setAuthModalOpen] = useState(false);
  const [authMode, setAuthMode] = useState("login");
  const [limitModalOpen, setLimitModalOpen] = useState(false);
  const [profileOpen, setProfileOpen] = useState(false);
  const [statusModal, setStatusModal] = useState(null);

  const [showHistory, setShowHistory] = useState(false);
  const [dbLogs, setDbLogs] = useState([]);
  const [selectedHistory, setSelectedHistory] = useState(null);

  const [showSearch, setShowSearch] = useState(false);
  const [searchQuery, setSearchQuery] = useState("");
  const [searchResults, setSearchResults] = useState([]);
  const [searchLoading, setSearchLoading] = useState(false);
  const [searchError, setSearchError] = useState("");
  const [searchTouched, setSearchTouched] = useState(false);
  const [expandedIndex, setExpandedIndex] = useState(null);

  const chatEndRef = useRef(null);
  const attachmentInputRef = useRef(null);
  const authHandledRef = useRef(false);

  const persistMessages = (next, sid = sessionId) => {
    localStorage.setItem(`chatMessages-${sid}`, JSON.stringify(next));
  };

  const openAuth = (mode = "login") => {
    setAuthMode(mode);
    setAuthModalOpen(true);
  };

  const guestLimitReached = !isAuthenticated && msgCount >= GUEST_MESSAGE_LIMIT;
  const guestMessagesLeft = Math.max(0, GUEST_MESSAGE_LIMIT - msgCount);

  const showLimitModal = () => setLimitModalOpen(true);

  const openAuthFromLimit = (mode = "login") => {
    setLimitModalOpen(false);
    openAuth(mode);
  };

  const requireAuth = (message, mode = "login") => {
    setMessages((prev) => [...prev, { role: "ai", text: message }]);
    openAuth(mode);
  };

  useEffect(() => {
    if (isAuthenticated) {
      setLimitModalOpen(false);
      return;
    }
    if (msgCount >= GUEST_MESSAGE_LIMIT) {
      setLimitModalOpen(true);
    }
  }, [isAuthenticated, msgCount]);

  useEffect(() => {
    const saved = readStoredMessages(sessionId);
    setMessages(saved || [INITIAL_AI_MESSAGE]);
  }, [sessionId]);

  useEffect(() => {
    if (messages.length) persistMessages(messages, sessionId);
  }, [messages, sessionId]);

  useEffect(() => {
    chatEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, loading]);

  useEffect(() => {
    const params = new URLSearchParams(location.search);
    const authParam = params.get("auth");
    if (authParam === "login" || authParam === "register") {
      openAuth(authParam);
      navigate("/", { replace: true });
    }
  }, [location.search, navigate]);

  useEffect(() => {
    if (authHandledRef.current) return;
    const params = new URLSearchParams(location.search);
    if (params.get("login") === "true" && isAuthenticated) {
      authHandledRef.current = true;
      const email = localStorage.getItem("userEmail");
      const freshSessionId = localStorage.getItem(sessionKeyFor(email));
      if (freshSessionId) {
        setSessionId(freshSessionId);
        setMessages(readStoredMessages(freshSessionId) || [INITIAL_AI_MESSAGE]);
      }
      setStatusModal({
        type: "success",
        title: "Welcome back!",
        message: "You're signed in with a fresh chat session. Ask me anything.",
      });
      navigate("/", { replace: true });
    }
  }, [location.search, isAuthenticated, navigate]);

  const handleAuthSuccess = (mode) => {
    setLimitModalOpen(false);
    const email = localStorage.getItem("userEmail");
    const freshSessionId = localStorage.getItem(sessionKeyFor(email));
    if (freshSessionId) {
      setSessionId(freshSessionId);
      setMessages([INITIAL_AI_MESSAGE]);
      persistMessages([INITIAL_AI_MESSAGE], freshSessionId);
    }
    setMsgCount(0);
    setStatusModal({
      type: "success",
      title: mode === "register" ? "Account created!" : "Signed in successfully",
      message: mode === "register"
        ? "Your account is ready. You can upload files and save chat history now."
        : "Welcome back! Your new chat session is ready.",
    });
  };

  const startNewChat = async () => {
    const token = getAuthToken();
    if (token && sessionId) {
      try {
        await apiRequest(`/rag/session/${encodeURIComponent(sessionId)}`, {
          method: "DELETE",
          token,
        });
      } catch {
        /* non-blocking */
      }
    }

    const email = localStorage.getItem("userEmail");
    const newId = startFreshSession(email);
    setSessionId(newId);
    setMessages([INITIAL_AI_MESSAGE]);
    setSelectedFiles([]);
    setInput("");
    setSidebarOpen(false);
  };

  const handleLogout = () => {
    logout();
    const guestSessionId = localStorage.getItem("chatSessionId");
    setSessionId(guestSessionId);
    setMessages([INITIAL_AI_MESSAGE]);
    setSelectedFiles([]);
    setInput("");
    setMsgCount(0);
    setSidebarOpen(false);
    setStatusModal({
      type: "logout",
      title: "Signed out",
      message: "You've been logged out safely. Sign in again whenever you're ready.",
    });
  };

  const openHistory = async () => {
    if (!isAuthenticated) {
      requireAuth("Please sign in first to view your saved chat history.");
      return;
    }
    setShowHistory(true);
    try {
      const data = await apiRequest("/history", { token: getAuthToken() });
      setDbLogs(Array.isArray(data) ? data : []);
    } catch (err) {
      setDbLogs([]);
      setStatusModal({
        type: "error",
        title: "Couldn't load history",
        message: friendlyChatError(err),
      });
    }
  };

  const deleteHistoryItem = async (id) => {
    try {
      await apiRequest(`/delete-history/${id}`, { method: "DELETE", token: getAuthToken() });
      setDbLogs((prev) => prev.filter((log) => log._id !== id));
    } catch (err) {
      setStatusModal({
        type: "error",
        title: "Delete failed",
        message: friendlyChatError(err),
      });
    }
  };

  const handleSearch = async () => {
    const query = searchQuery.trim();
    setSearchTouched(true);
    setSearchError("");
    if (!query) {
      setSearchError("Please enter a search phrase.");
      return;
    }
    if (!isAuthenticated) {
      setSearchError("Please sign in first to search your saved conversations.");
      return;
    }

    setSearchLoading(true);
    try {
      const data = await apiRequest(`/search-memory?query=${encodeURIComponent(query)}`, {
        token: getAuthToken(),
      });
      setSearchResults(Array.isArray(data) ? data : []);
      if (!data?.length) setSearchError("No matching conversations found.");
    } catch (err) {
      setSearchResults([]);
      setSearchError(friendlyChatError(err));
    } finally {
      setSearchLoading(false);
    }
  };

  const handleAttachmentPick = (e) => {
    const files = Array.from(e.target.files || []);
    const allowed = [".pdf", ".png", ".jpg", ".jpeg", ".webp", ".gif", ".txt", ".md", ".log", ".json", ".yaml", ".yml", ".csv", ".xml", ".html", ".htm", ".docx", ".doc", ".pptx"];
    const valid = files.filter((f) => allowed.some((ext) => f.name.toLowerCase().endsWith(ext)));
    if (!valid.length) {
      setMessages((prev) => [...prev, {
        role: "ai",
        text: "That file type isn't supported yet. Please upload PDF, images, TXT, DOCX, PPTX, or similar documents.",
      }]);
      return;
    }
    setSelectedFiles((prev) => [...prev, ...valid.filter((f) => !prev.some((p) => p.name === f.name))]);
    if (attachmentInputRef.current) attachmentInputRef.current.value = "";
  };

  const askAI = async () => {
    const question = input.trim();
    const hasFiles = selectedFiles.length > 0;
    if (!question && !hasFiles) return;

    if (hasFiles && !isAuthenticated) {
      requireAuth("Please sign in first before uploading documents. That way your files stay private and searchable.");
      return;
    }

    if (!isAuthenticated && msgCount >= GUEST_MESSAGE_LIMIT) {
      showLimitModal();
      return;
    }

    const messageText = question || "Please analyze the attached document(s) and summarize the key information.";
    const filesToSend = [...selectedFiles];
    const priorConversation = messages
      .map((m) => ({
        role: m.role === "ai" ? "assistant" : "user",
        text: m.text,
      }))
      .slice(-10);

    setMessages((prev) => {
      const updated = [...prev, {
        role: "user",
        text: filesToSend.length
          ? `${messageText}${messageText ? "\n\n" : ""}[Attached: ${filesToSend.map((f) => f.name).join(", ")}]`
          : messageText,
      }];
      persistMessages(updated);
      return updated;
    });

    setInput("");
    if (filesToSend.length) {
      setSelectedFiles([]);
      if (attachmentInputRef.current) attachmentInputRef.current.value = "";
    }

    setLoading(true);
    setUploadingFiles(filesToSend.length > 0);

    let timeoutId;
    try {
      const controller = new AbortController();
      timeoutId = setTimeout(() => controller.abort(), filesToSend.length ? 180000 : 45000);
      const token = getAuthToken();
      const headers = token ? { Authorization: `Bearer ${token}` } : {};
      let body;

      if (filesToSend.length) {
        const form = new FormData();
        form.append("message", messageText);
        form.append("session_id", sessionId);
        if (user?.email) form.append("email", user.email);
        filesToSend.forEach((file) => form.append("file", file));
        body = form;
      } else {
        headers["Content-Type"] = "application/json";
        body = JSON.stringify({
          message: messageText,
          email: user?.email || null,
          session_id: sessionId,
          conversation: priorConversation,
        });
      }

      const res = await fetch(`${API_URL}${filesToSend.length ? "/chat/upload" : "/chat"}`, {
        method: "POST",
        headers,
        body,
        signal: controller.signal,
      });
      clearTimeout(timeoutId);

      const data = await res.json();
      if (!res.ok) {
        const detail = data?.detail || data?.reply || `Request failed (${res.status})`;
        throw new Error(hasFiles ? friendlyUploadError(detail) : detail);
      }

      if (!isAuthenticated) {
        const next = msgCount + 1;
        setMsgCount(next);
        localStorage.setItem("queryCount", String(next));
        if (next >= GUEST_MESSAGE_LIMIT) {
          setLimitModalOpen(true);
        }
      }

      setMessages((prev) => {
        const updated = [...prev, {
          role: "ai",
          text: data.reply || "I couldn't generate a response.",
          sources: Array.isArray(data.sources) ? data.sources : [],
        }];
        persistMessages(updated);
        return updated;
      });
    } catch (err) {
      setMessages((prev) => {
        const updated = [...prev, { role: "ai", text: friendlyChatError(err) }];
        persistMessages(updated);
        return updated;
      });
    } finally {
      if (timeoutId) clearTimeout(timeoutId);
      setLoading(false);
      setUploadingFiles(false);
    }
  };

  const handleComposerKeyDown = (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      askAI();
    }
  };

  return (
    <div className="app-shell">
      {sidebarOpen && <div className="sidebar-overlay" onClick={() => setSidebarOpen(false)} role="presentation" />}

      <aside className={`sidebar ${sidebarOpen ? "open" : ""}`}>
        <div className="sidebar-brand">
          <div className="brand-logo">AI</div>
          <div className="brand-copy">
            <strong>AI Chat</strong>
          </div>
        </div>

        <div className="sidebar-nav">
          <button type="button" className="nav-btn" onClick={startNewChat}>
            <div><strong>New chat</strong><small>Start fresh</small></div>
          </button>
          <button type="button" className="nav-btn" onClick={() => { setShowSearch(true); setSidebarOpen(false); }}>
            <div><strong>Search memory</strong><small>Find past answers</small></div>
          </button>
          <button type="button" className="nav-btn" onClick={() => { openHistory(); setSidebarOpen(false); }}>
            <div><strong>History</strong><small>Saved conversations</small></div>
          </button>
        </div>

        <div className="sidebar-footer">
          {!isAuthenticated ? (
            <>
              <button type="button" className="btn btn-primary btn-full" onClick={() => openAuth("login")}>Sign in</button>
              <button type="button" className="btn btn-secondary btn-full" onClick={() => openAuth("register")}>Sign up</button>
            </>
          ) : (
            <>
              <div className="user-card">
                <div className="avatar">
                  {user?.avatar_url ? <img src={user.avatar_url} alt="" /> : (user?.username || "U").charAt(0).toUpperCase()}
                </div>
                <div>
                  <strong>{user?.username}</strong>
                  <span>{user?.email}</span>
                </div>
              </div>
              <div className="user-actions">
                <button type="button" className="btn btn-ghost" onClick={() => setProfileOpen(true)}>Profile</button>
                <button type="button" className="btn btn-ghost" onClick={handleLogout}>Sign out</button>
              </div>
            </>
          )}
        </div>
      </aside>

      <main className="chat-main">
        <header className="chat-topbar">
          <div style={{ display: "flex", alignItems: "center", gap: "0.75rem" }}>
            <button type="button" className="icon-btn mobile-menu-btn" onClick={() => setSidebarOpen(true)} aria-label="Open menu">
              <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" strokeWidth="2"><line x1="4" y1="6" x2="20" y2="6" /><line x1="4" y1="12" x2="20" y2="12" /><line x1="4" y1="18" x2="20" y2="18" /></svg>
            </button>
            <div>
              <h1>Nova AI Assistant</h1>
              <p>{isAuthenticated ? "Unlimited chat · Uploads enabled" : `${guestMessagesLeft} free message${guestMessagesLeft === 1 ? "" : "s"} left`}</p>
            </div>
          </div>
          <div className="topbar-actions">
            <button type="button" className="icon-btn" onClick={toggleTheme} aria-label="Toggle theme">
              {isDark ? (
                <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" strokeWidth="2"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z" /></svg>
              ) : (
                <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" strokeWidth="2"><circle cx="12" cy="12" r="5" /><line x1="12" y1="1" x2="12" y2="3" /><line x1="12" y1="21" x2="12" y2="23" /></svg>
              )}
            </button>
          </div>
        </header>

        <section className="messages-panel">
          {messages.map((msg, i) => (
            <div key={`${i}-${msg.role}`} className={`message-row ${msg.role}`}>
              <div className={`message-bubble ${msg.role === "ai" ? "ai-bubble-rich" : ""}`}>
                <MessageMarkdown
                  text={msg.text}
                  role={msg.role}
                  showActions={msg.role !== "ai" || msg.text !== INITIAL_AI_MESSAGE.text}
                />
                {msg.role === "ai" && Array.isArray(msg.sources) && msg.sources.length > 0 && (
                  <div className="source-block">
                    <strong>Sources</strong>
                    <ul>
                      {msg.sources.slice(0, 5).map((s, idx) => (
                        <li key={`${i}-src-${idx}`}>
                          {typeof s === "string" ? s : `${s.source || "unknown"}${s.page ? ` (p.${s.page})` : ""}`}
                        </li>
                      ))}
                    </ul>
                  </div>
                )}
              </div>
            </div>
          ))}
          {loading && (
            <div className="message-row ai">
              <AiLoadingBubble label={uploadingFiles ? "Reading your file" : "AI is thinking"} />
            </div>
          )}
          <div ref={chatEndRef} />
        </section>

        <div className="composer-wrap">
          <div className="composer">
            <button
              type="button"
              className="icon-btn"
              onClick={() => {
                if (guestLimitReached) {
                  showLimitModal();
                  return;
                }
                attachmentInputRef.current?.click();
              }}
              aria-label="Attach file"
            >
              <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" strokeWidth="2"><path d="M21.44 11.05l-9.19 9.19a6 6 0 01-8.49-8.49l9.19-9.19a4 4 0 115.66 5.66l-9.2 9.2a2 2 0 11-2.83-2.83l8.49-8.48" /></svg>
            </button>
            <input ref={attachmentInputRef} type="file" multiple hidden accept=".pdf,.png,.jpg,.jpeg,.webp,.gif,.txt,.md,.log,.json,.yaml,.yml,.csv,.xml,.html,.htm,.docx,.doc,.pptx" onChange={handleAttachmentPick} />
            <div className="composer-input-stack">
              {selectedFiles.length > 0 && (
                <div className="attachment-row">
                  {selectedFiles.map((f) => (
                    <span className="chip" key={f.name}>
                      {f.name}
                      <button type="button" onClick={() => setSelectedFiles((prev) => prev.filter((x) => x.name !== f.name))}>×</button>
                    </span>
                  ))}
                </div>
              )}
              <textarea
                className="chat-input"
                rows={1}
                value={input}
                onChange={(e) => setInput(e.target.value)}
                onKeyDown={handleComposerKeyDown}
                placeholder={guestLimitReached ? "Sign in or sign up to continue chatting..." : "Ask anything, or attach a document..."}
                disabled={loading || guestLimitReached}
              />
            </div>
            <button type="button" className="btn btn-primary" onClick={askAI} disabled={loading || uploadingFiles || guestLimitReached}>
              {uploadingFiles ? "Uploading..." : loading ? "Sending..." : "Send"}
            </button>
          </div>
        </div>
      </main>

      <LimitReachedModal
        open={limitModalOpen && !isAuthenticated}
        onClose={() => setLimitModalOpen(false)}
        onSignIn={() => openAuthFromLimit("login")}
        onSignUp={() => openAuthFromLimit("register")}
        limit={GUEST_MESSAGE_LIMIT}
      />

      <AuthModal
        open={authModalOpen}
        onClose={() => setAuthModalOpen(false)}
        initialMode={authMode}
        onSuccess={handleAuthSuccess}
        googleEnabled={googleEnabled}
      />

      <ProfileModal open={profileOpen} onClose={() => setProfileOpen(false)} />

      <StatusModal
        open={Boolean(statusModal)}
        onClose={() => setStatusModal(null)}
        type={statusModal?.type || "info"}
        title={statusModal?.title || ""}
        message={statusModal?.message || ""}
      />

      <HistoryModal
        open={showHistory}
        onClose={() => { setShowHistory(false); setSelectedHistory(null); }}
        logs={dbLogs}
        selected={selectedHistory}
        onSelect={setSelectedHistory}
        onClearSelection={() => setSelectedHistory(null)}
        onDelete={deleteHistoryItem}
      />

      <SearchModal
        open={showSearch}
        onClose={() => setShowSearch(false)}
        query={searchQuery}
        onQueryChange={setSearchQuery}
        onSearch={handleSearch}
        loading={searchLoading}
        error={searchError}
        touched={searchTouched}
        results={searchResults}
        expandedIndex={expandedIndex}
        onToggleExpand={(i) => setExpandedIndex(expandedIndex === i ? null : i)}
        onOpenInChat={(res) => {
          setMessages((prev) => {
            const updated = [...prev, { role: "user", text: res.user_query }, { role: "ai", text: res.ai_response }];
            persistMessages(updated);
            return updated;
          });
          setShowSearch(false);
        }}
      />
    </div>
  );
}
