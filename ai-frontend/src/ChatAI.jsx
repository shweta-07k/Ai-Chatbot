import React, { useState, useEffect, useRef } from 'react';
import ReactMarkdown from 'react-markdown';
import './ChatAI.css';
import { toast } from 'react-toastify';
import 'react-toastify/dist/ReactToastify.css';
import { useLocation, useNavigate } from 'react-router-dom';

const ChatAI = () => {
    const toastIcon = (type) => {
        const baseProps = { width: 18, height: 18, strokeWidth: 2, stroke: '#fff', fill: 'none' };
        if (type === 'success') {
            return (
                <svg {...baseProps} viewBox="0 0 24 24">
                    <path d="M9 12l2 2 4-4" />
                    <circle cx="12" cy="12" r="9" />
                </svg>
            );
        }
        if (type === 'error') {
            return (
                <svg {...baseProps} viewBox="0 0 24 24">
                    <line x1="15" y1="9" x2="9" y2="15" />
                    <line x1="9" y1="9" x2="15" y2="15" />
                    <circle cx="12" cy="12" r="9" />
                </svg>
            );
        }
        return (
            <svg {...baseProps} viewBox="0 0 24 24">
                <circle cx="12" cy="12" r="9" />
                <line x1="12" y1="8" x2="12" y2="12" />
                <circle cx="12" cy="16" r="1" fill="#fff" />
            </svg>
        );
    };
    const [userName, setUserName] = useState("User");
    const [input, setInput] = useState('');
    const [messages, setMessages] = useState([
        { role: 'ai', text: 'Hello! I am your AI Data Assistant. How can I help you today?' }
    ]);
    const [loading, setLoading] = useState(false);
    const [uploadingFiles, setUploadingFiles] = useState(false);
    const [selectedFiles, setSelectedFiles] = useState([]);
    const chatEndRef = useRef(null);
    const attachmentInputRef = useRef(null);
    const toastShownRef = useRef(false);
    const [theme, setTheme] = useState('dark');
    const [sessionId] = useState(() => `session_${Date.now()}_${Math.random().toString(36).substr(2, 9)}`);

    const [showHistory, setShowHistory] = useState(false);
    const [selectedHistory, setSelectedHistory] = useState(null);
    const [dbLogs, setDbLogs] = useState([]);

    const [showSearch, setShowSearch] = useState(false);
    const [searchQuery, setSearchQuery] = useState("");
    const [searchResults, setSearchResults] = useState([]);
    const [searchLoading, setSearchLoading] = useState(false);
    const [searchError, setSearchError] = useState("");
    const [searchTouched, setSearchTouched] = useState(false);

    const navigate = useNavigate();
    const location = useLocation();

    const handleMemorySelect = (result) => {
        const userQuery = result.user_query || "Memory query";
        const aiResponse = result.ai_response || "No response available.";

        setMessages(prev => [
            ...prev,
            { role: 'user', text: userQuery },
            { role: 'ai', text: aiResponse }
        ]);

        setShowSearch(false);
        setExpandedIndex(null);
        setSearchError("");
        setSearchLoading(false);
        setSearchTouched(false);
        setSearchQuery("");
    };
    const [msgCount, setMsgCount] = useState(
        Number(localStorage.getItem("queryCount")) || 0
    ); // Track messages
    const [showRegPopup, setShowRegPopup] = useState(false)
    const [isAuthenticated, setIsAuthenticated] = useState(
        !!localStorage.getItem("token")
    );

    const [expandedIndex, setExpandedIndex] = useState(null);     //for search index
    const [showDropdown, setShowDropdown] = useState(false);      // for user section dropdown




    // Fetch History from MongoDB
    const openHistory = async () => {
        setShowHistory(true);
        try {
            const email = localStorage.getItem("userEmail");

            const res = await fetch("http://127.0.0.1:8000/history", {
                headers: {
                    "Authorization": `Bearer ${localStorage.getItem("token")}`
                }
            });
            console.log("STORED TOKEN:", localStorage.getItem("token"));

            if (!res.ok) {
                throw new Error("Unauthorized or failed");
            }
            const data = await res.json();
            setDbLogs(data);
        } catch (err) {
            console.error("DB Fetch Error:", err);
        }
    };


    // Delete history one 
    const deleteSelected = async (id) => {
        try {
            const res = await fetch(
                `http://127.0.0.1:8000/delete-history/${id}`,
                {
                    method: "DELETE",
                    headers: {
                        "Authorization": `Bearer ${localStorage.getItem("token")}`
                    }
                }
            );

            if (res.ok) {
                setDbLogs(prevLogs => prevLogs.filter(log => log._id !== id));
            } else {
                console.error("Delete failed");
            }

        } catch (err) {
            console.error("Failed to delete:", err);
            alert("Database delete failed.");
        }
    };

    const selectHistoryEntry = (log) => {
        setSelectedHistory(log);
    };

    const closeHistoryDetail = () => {
        setSelectedHistory(null);
    };

    // search history
    const handleSearch = async () => {
        const query = searchQuery.trim();
        setSearchTouched(true);
        setSearchError("");

        if (!query) {
            setSearchResults([]);
            setSearchError("Please enter a search query.");
            return;
        }

        setSearchLoading(true);
        setSearchResults([]);

        try {
            const res = await fetch(`http://127.0.0.1:8000/search-memory?query=${encodeURIComponent(query)}`,
                {
                    headers: {
                        "Authorization": `Bearer ${localStorage.getItem("token")}`
                    }
                });

            const data = await res.json();

            if (!res.ok) {
                const message = data?.detail || `Search failed with status ${res.status}`;
                throw new Error(message);
            }

            if (!Array.isArray(data)) {
                throw new Error("Unexpected server response.");
            }

            if (data.length === 0) {
                setSearchError("No memory results found for this query.");
            }

            setSearchResults(data);
        } catch (err) {
            console.error("Search failed:", err);
            setSearchError(err.message || "Search failed. Please try again.");
            setSearchResults([]);
        } finally {
            setSearchLoading(false);
        }
    };

    // const userName = localStorage.getItem("userName") || "User";
    useEffect(() => {
        const name = localStorage.getItem("username");
        if (name) {
            setUserName(name);
        }
    }, []);


    useEffect(() => {
        if (toastShownRef.current) return;

        const params = new URLSearchParams(location.search);

        let shouldRedirect = false;

        if (params.get('registered') === 'true') {
            toast.success("Registration Successful!", { icon: toastIcon('success') });
            localStorage.setItem("queryCount", 0);
            shouldRedirect = true;
        }

        if (params.get('login') === 'true') {
            toast.success("Login Successful!", { icon: toastIcon('success') });
            localStorage.setItem("queryCount", 0);

            const name = localStorage.getItem("username"); // Ã°Å¸â€˜Ë† read again
            if (name) setUserName(name);
            setIsAuthenticated(!!localStorage.getItem("token"));
            shouldRedirect = true;
        }

        if (shouldRedirect) {
            toastShownRef.current = true;
            navigate('/', { replace: true });
        }

    }, [location]);

    useEffect(() => {
        const handleClickOutside = () => setShowDropdown(false);

        if (showDropdown) {
            document.addEventListener("click", handleClickOutside);
        }

        return () => {
            document.removeEventListener("click", handleClickOutside);
        };
    }, [showDropdown]);

    useEffect(() => {
        chatEndRef.current?.scrollIntoView({ behavior: 'smooth' });
    }, [messages]);

    const handleAttachmentPick = (e) => {
        const files = Array.from(e.target.files || []);
        if (!files.length) return;

        const allowed = ['application/pdf', 'image/png', 'image/jpeg'];
        const valid = files.filter((f) => allowed.includes(f.type));
        if (!valid.length) {
            setMessages(prev => [...prev, {
                role: 'ai',
                text: 'Please upload only PDF, PNG, JPG, or JPEG files.'
            }]);
            return;
        }
        uploadSelectedFiles(valid)
            .then((uploadedNames) => {
                setSelectedFiles((prev) => {
                    const existing = new Set(prev.map((f) => f.name || String(f)));
                    const merged = [...prev];
                    for (const name of uploadedNames) {
                        if (!existing.has(name)) {
                            merged.push({ name });
                        }
                    }
                    return merged;
                });

                setMessages((prev) => [
                    ...prev,
                    { role: 'user', text: `Uploaded file(s): ${uploadedNames.join(", ")}` },
                    { role: 'ai', text: 'Files indexed. Ask your question related to these files.' }
                ]);
            })
            .catch((err) => {
                setMessages((prev) => [
                    ...prev,
                    { role: 'ai', text: `File upload failed. ${err?.message || ""}`.trim() }
                ]);
            })
            .finally(() => {
                if (attachmentInputRef.current) {
                    attachmentInputRef.current.value = "";
                }
            });
    };

    const uploadSelectedFiles = async (files) => {
        if (!files.length) return [];
        const token = localStorage.getItem("token");
        if (!token) {
            throw new Error("Please login to upload files.");
        }

        setUploadingFiles(true);
        const uploaded = [];
        try {
            for (const file of files) {
                const form = new FormData();
                form.append("file", file);
                form.append("source_label", "chat_attachment");

                const res = await fetch("http://127.0.0.1:8000/rag/ingest-file", {
                    method: "POST",
                    headers: {
                        "Authorization": `Bearer ${token}`
                    },
                    body: form
                });
                const data = await res.json().catch(() => ({}));
                if (!res.ok) {
                    throw new Error(data?.detail || `Upload failed (${res.status})`);
                }
                uploaded.push(file.name);
            }
        } finally {
            setUploadingFiles(false);
        }

        return uploaded;
    };

    const removeSelectedFile = (nameToRemove) => {
        setSelectedFiles((prev) => prev.filter((f) => (f.name || f) !== nameToRemove));
    };

    const askAI = async () => {
        if (!input.trim()) return;

        const nextCount = msgCount + 1;

        if (!isAuthenticated && nextCount > 3) {
            setShowRegPopup(true);
            return;
        }

        const userMsg = { role: 'user', text: input };
        setMessages(prev => [...prev, userMsg]);

        setMsgCount(nextCount);
        localStorage.setItem("queryCount", nextCount);

        setInput('');
        setLoading(true);

        try {
            const res = await fetch("http://127.0.0.1:8000/chat", {
                method: "POST",
                headers: {
                    "Content-Type": "application/json",
                    "Authorization": `Bearer ${localStorage.getItem("token")}`
                },
                body: JSON.stringify({
                    message: input,
                    email: localStorage.getItem("userEmail"),
                    session_id: sessionId
                }),
            });

            const data = await res.json();

            setMessages(prev => [...prev, {
                role: 'ai',
                text: data.reply || "AI Error",
                sources: Array.isArray(data.sources) ? data.sources : []
            }]);

        } catch (err) {
            setMessages(prev => [...prev, {
                role: 'ai',
                text: `Connection lost. ${err?.message || ""}`.trim()
            }]);
        }

        setLoading(false);
    }
    const handleLogout = () => {
        localStorage.removeItem("token");
        localStorage.removeItem("username");
        localStorage.removeItem("userEmail");
        localStorage.removeItem("queryCount");

        setIsAuthenticated(false);
        setUserName("User");
        setMessages([
            { role: 'ai', text: 'Hello! I am your AI Data Assistant. How can I help you today?' }
        ]);
        setMsgCount(0);
        setShowDropdown(false);

        navigate('/', { replace: true });
        window.location.reload();
    };




    return (
        <div className={`ai-container ${theme}`}>
            <div className="ai-sidebar">
                <div className="sidebar-header">
                    <div className="status-dot"></div>
                    <span style={{ fontWeight: 'bold' }}>AI CLUSTER</span>
                    <div
                        className="theme-toggle"
                        onClick={() => setTheme(theme === 'dark' ? 'light' : 'dark')}
                    >
                        <svg
                            className="theme-icon"
                            viewBox="0 0 24 24"
                            fill="none"
                            stroke="currentColor"
                            strokeWidth="2"
                            strokeLinecap="round"
                            strokeLinejoin="round"
                        >
                            {theme === 'dark' ? (
                                <path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z" />
                            ) : (
                                <g>
                                    <circle cx="12" cy="12" r="5" />
                                    <line x1="12" y1="1" x2="12" y2="3" />
                                    <line x1="12" y1="21" x2="12" y2="23" />
                                    <line x1="4.22" y1="4.22" x2="5.64" y2="5.64" />
                                    <line x1="18.36" y1="18.36" x2="19.78" y2="19.78" />
                                    <line x1="1" y1="12" x2="3" y2="12" />
                                    <line x1="21" y1="12" x2="23" y2="12" />
                                    <line x1="4.22" y1="19.78" x2="5.64" y2="18.36" />
                                    <line x1="18.36" y1="5.64" x2="19.78" y2="4.22" />
                                </g>
                            )}
                        </svg>
                    </div>
                </div>
                <div className="sidebar-section">
                    <div className="sidebar-section-title">Memory Tools</div>
                    <button
                        className="sidebar-card-btn"
                        onClick={() => {
                            setSearchQuery("");
                            setSearchResults([]);
                            setSearchError("");
                            setSearchTouched(false);
                            setExpandedIndex(null);
                            setShowSearch(true);
                        }}
                    >
                        <span className="sidebar-icon">
                            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                                <circle cx="11" cy="11" r="7" />
                                <line x1="16.65" y1="16.65" x2="21" y2="21" />
                            </svg>
                        </span>
                        <div>
                            <strong>Search Memory</strong>
                            <small>Find older conversations fast</small>
                        </div>
                    </button>
                    <button className="sidebar-card-btn" onClick={openHistory}>
                        <span className="sidebar-icon">
                            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                                <circle cx="12" cy="12" r="8" />
                                <polyline points="12 6 12 12 16 14" />
                            </svg>
                        </span>
                        <div>
                            <strong>Chat History</strong>
                            <small>Review your recent AI sessions</small>
                        </div>
                    </button>
                </div>
                <div className="sidebar-bottom">
                    {!isAuthenticated ? (
                        <div style={{ display: "flex", flexDirection: "column", gap: "10px" }}>
                            <button className="auth-btn" onClick={() => navigate('/login')}>
                                Login
                            </button>
                            <button className="auth-btn" onClick={() => navigate('/register')}>
                                Register
                            </button>
                        </div>
                    ) : (
                        <div className="user-profile-section" style={{ position: "relative" }}>
                            <div
                                className="user-info"
                                onClick={(e) => {
                                    e.stopPropagation();
                                    setShowDropdown(!showDropdown);
                                }}
                                style={{ cursor: "pointer", display: "flex", alignItems: "center", gap: "10px" }}
                            >
                                {/* Avatar */}
                                <div
                                    style={{
                                        width: "35px",
                                        height: "35px",
                                        borderRadius: "50%",
                                        background: "#585480",
                                        display: "flex",
                                        alignItems: "center",
                                        justifyContent: "center",
                                        color: "#fff",
                                        fontWeight: "bold"
                                    }}
                                >
                                    {userName?.charAt(0).toUpperCase()}
                                </div>

                                {/* Name */}
                                <span className="user-name">Welcome, {userName}!</span>
                            </div>

                            {/* Dropdown */}
                            {showDropdown && (
                                <div
                                    style={{
                                        position: "absolute",
                                        bottom: "50px",
                                        left: "0",
                                        background: "#1f2937",
                                        borderRadius: "8px",
                                        padding: "10px",
                                        width: "180px",
                                        boxShadow: "0 0 10px rgba(0,0,0,0.5)",
                                        zIndex: 10
                                    }}
                                >
                                    <p style={{ color: "#9ca3af", fontSize: "12px" }}>Signed in as</p>
                                    <p style={{ color: "#fff", fontWeight: "600" }}>{userName}</p>

                                    <hr style={{ borderColor: "#374151", margin: "8px 0" }} />

                                    <button
                                        onClick={handleLogout}
                                        style={{
                                            width: "100%",
                                            padding: "8px",
                                            background: "#4f4da1",
                                            border: "none",
                                            borderRadius: "6px",
                                            color: "#fff",
                                            cursor: "pointer"
                                        }}
                                    >
                                        Logout
                                    </button>
                                </div>
                            )}
                        </div>
                    )}
                </div>


            </div>

            <div className="chat-card">
                <div className="chat-header">
                    <div className="status-dot"></div>
                    <svg className="chat-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                        <path d="M9.75 17L9 20l-1 1h8l-1-1-.75-3M3 13h18M5 17h14a2 2 0 002-2V5a2 2 0 00-2-2H5a2 2 0 00-2 2v10a2 2 0 002 2z" />
                    </svg>
                    <h2 className="chat-title">AI Infrastructure Hub</h2>
                </div>

                <div className="message-area">
                    {messages.map((msg, i) => (
                        <div key={i} className={msg.role === 'user' ? 'user-row' : 'ai-row'}>
                            <div className={msg.role === 'user' ? 'user-bubble' : 'ai-bubble'}>
                                <ReactMarkdown>{msg.text}</ReactMarkdown>
                                {msg.role === 'ai' && Array.isArray(msg.sources) && msg.sources.length > 0 && (
                                    <div className="source-block">
                                        <div className="source-title">Sources</div>
                                        <ul className="source-list">
                                            {msg.sources.slice(0, 5).map((s, idx) => (
                                                <li key={`${i}-src-${idx}`}>
                                                    {typeof s === 'string'
                                                        ? s
                                                        : `${s.source || 'unknown'}${s.page ? ` (p.${s.page})` : ''}${s.score ? ` [score ${s.score}]` : ''}`}
                                                </li>
                                            ))}
                                        </ul>
                                    </div>
                                )}
                            </div>
                        </div>
                    ))}
                    {loading && (
                        <div className="loading-row">
                            <div className="loading-bubble">AI is calculating...</div>
                        </div>
                    )}
                    <div ref={chatEndRef} />
                </div>

                {showRegPopup && (
                    <div className="modal-overlay">
                        <div className="modal-content auth-card" style={{ textAlign: 'center' }}>
                            <h2 className="auth-title">Limit Reached</h2>
                            <p style={{ color: '#aaa', margin: '15px 0' }}>
                                Please register your AI account to unlock unlimited data processing.
                            </p>
                            <button
                                className="submit-btn"
                                onClick={() => navigate('/register')}
                            >
                                Register AI Account
                            </button>
                        </div>
                    </div>
                )}

                <div className="input-area">
                    <input
                        ref={attachmentInputRef}
                        type="file"
                        accept=".pdf,.png,.jpg,.jpeg"
                        multiple
                        style={{ display: "none" }}
                        onChange={handleAttachmentPick}
                    />
                    <button
                        type="button"
                        className="attach-btn"
                        title="Attach PDF or image"
                        onClick={() => attachmentInputRef.current?.click()}
                    >
                        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                            <path d="M21.44 11.05l-9.19 9.19a6 6 0 01-8.49-8.49l9.19-9.19a4 4 0 115.66 5.66l-9.2 9.2a2 2 0 11-2.83-2.83l8.49-8.48" />
                        </svg>
                    </button>
                    <div className="chat-composer">
                        {selectedFiles.length > 0 && (
                            <div className="attachment-chip-row">
                                {selectedFiles.map((f, idx) => {
                                    const fileName = f.name || f;
                                    return (
                                        <span className="attachment-chip" key={`${fileName}-${idx}`}>
                                            <span className="attachment-chip-name">{fileName}</span>
                                            <button
                                                type="button"
                                                className="attachment-chip-remove"
                                                onClick={() => removeSelectedFile(fileName)}
                                                aria-label={`Remove ${fileName}`}
                                                title="Remove file"
                                            >x</button>
                                        </span>
                                    );
                                })}
                            </div>
                        )}
                        <input
                            className="chat-input"
                            value={input}
                            onChange={(e) => setInput(e.target.value)}
                            onKeyPress={(e) => e.key === 'Enter' && askAI()}
                            placeholder="Command the AI..."
                        />
                    </div>
                    <button onClick={askAI} className="send-btn" disabled={uploadingFiles}>
                        {uploadingFiles ? "Uploading..." : "Ask Me"}
                    </button>
                </div>

            </div>
            {/* MONGO HISTORY MODAL */}
            {showHistory && (
                <div className="modal-overlay" onClick={() => setShowHistory(false)}>
                    <div className="modal-content modal-panel" onClick={(e) => e.stopPropagation()}>
                        <div className="modal-header modal-panel-header">
                            <div>
                                <h2>Chat History</h2>
                                <p>Browse your saved conversations with richer context.</p>
                            </div>
                            <button className="close-btn" onClick={() => setShowHistory(false)}>&times;</button>
                        </div>
                        <div className="history-list">
                            {dbLogs.length > 0 ? dbLogs.map((log, i) => (
                                <div
                                    key={i}
                                    className="history-card history-card-large"
                                    onClick={() => selectHistoryEntry(log)}
                                    style={{ cursor: 'pointer' }}
                                >
                                    <button
                                        className="inline-delete-btn"
                                        onClick={(e) => {
                                            e.stopPropagation();
                                            deleteSelected(log._id);
                                        }}
                                        title="Delete this entry"
                                    >
                                        Ã°Å¸â€”â€˜Ã¯Â¸Â
                                    </button>
                                    <h4>Query: {log.user_query}</h4>
                                    <p><strong>Response:</strong> {log.ai_response.substring(0, 100)}...</p>
                                </div>
                            )) : <p style={{ color: '#94a3b8' }}>Fetching data from cloud...</p>}
                        </div>
                        {selectedHistory && (
                            <div className="history-detail-overlay">
                                <div className="history-detail-header">
                                    <div>
                                        <h3>Chat Detail</h3>
                                        <p>Selected question and response shown in a focused card.</p>
                                    </div>
                                    <button className="detail-close-btn" onClick={closeHistoryDetail}>&times;</button>
                                </div>
                                <div className="history-detail-content">
                                    <div className="history-detail-row">
                                        <span>Question</span>
                                        <p>{selectedHistory.user_query}</p>
                                    </div>
                                    <div className="history-detail-row">
                                        <span>Answer</span>
                                        <p>{selectedHistory.ai_response}</p>
                                    </div>
                                    {selectedHistory.created_at && (
                                        <div className="history-detail-row">
                                            <span>Saved</span>
                                            <p>{new Date(selectedHistory.created_at).toLocaleString()}</p>
                                        </div>
                                    )}
                                </div>
                            </div>
                        )}
                    </div>
                </div>
            )}

            {/* Search Modal */}
            {showSearch && (
                <div className="modal-overlay" onClick={() => setShowSearch(false)}>
                    <div className="modal-content modal-panel" onClick={(e) => e.stopPropagation()}>
                        <div className="modal-header modal-panel-header">
                            <div>
                                <h2>Search Memory</h2>
                                <p>Type an idea or keyword to pull up related conversations.</p>
                            </div>
                            <button
                                className="close-btn"
                                onClick={() => {
                                    setShowSearch(false);
                                    setSearchLoading(false);
                                    setSearchError("");
                                }}
                            >&times;</button>
                        </div>

                        <div className="input-area search-input-row">
                            <input
                                className="chat-input"
                                placeholder="Search by meaning (e.g., 'What did we say about databases?')..."
                                value={searchQuery}
                                onChange={(e) => setSearchQuery(e.target.value)}
                                onKeyPress={(e) => e.key === 'Enter' && handleSearch()}
                            />
                            <button onClick={handleSearch} className="send-btn">Search</button>
                        </div>

                        <div className="history-list" style={{ maxHeight: "300px", overflowY: "auto" }}>
                            {searchLoading && (
                                <p style={{ color: '#94a3b8', margin: '0' }}>Searching memory...</p>
                            )}
                            {!searchLoading && searchError && (
                                <p style={{ color: '#f87171', margin: '0' }}>{searchError}</p>
                            )}
                            {!searchLoading && !searchError && searchResults.length === 0 && searchTouched && (
                                <p style={{ color: '#94a3b8', margin: '0' }}>No memory results found for this query.</p>
                            )}
                            {!searchLoading && !searchError && searchResults.length === 0 && !searchTouched && (
                                <p style={{ color: '#94a3b8', margin: '0' }}>Enter a query and press Search to look through memory.</p>
                            )}
                            {searchResults.map((res, i) => (
                                <div
                                    key={i}
                                    className="history-card"
                                    onClick={() =>
                                        setExpandedIndex(expandedIndex === i ? null : i)
                                    }
                                    style={{
                                        borderLeft: "4px solid #10b981",
                                        padding: "10px",
                                        marginBottom: "10px",
                                        borderRadius: "8px",
                                        background: "#111",
                                        cursor: "pointer",
                                        transition: "0.2s"
                                    }}
                                    onMouseEnter={(e) => (e.currentTarget.style.background = "#1f2937")}
                                    onMouseLeave={(e) => (e.currentTarget.style.background = "#111")}
                                >
                                    <div style={{ fontSize: "11px", color: "#6b7280", marginTop: "4px" }}>
                                        {expandedIndex === i ? "Click to collapse Ã¢â€“Â²" : "Click to expand Ã¢â€“Â¼"}
                                    </div>
                                    {/* Question */}
                                    <div style={{ fontWeight: "600", color: "#10b981" }}>
                                        Q: {res.user_query}
                                    </div>

                                    {/* Answer */}
                                    <div
                                        style={{
                                            marginTop: "6px",
                                            fontSize: "13px",
                                            color: "#9ca3af",
                                            display: expandedIndex === i ? "block" : "-webkit-box",
                                            WebkitLineClamp: expandedIndex === i ? "unset" : 2,
                                            WebkitBoxOrient: "vertical",
                                            overflow: "hidden"
                                        }}
                                    >
                                        {res.ai_response}
                                    </div>

                                    <button
                                        type="button"
                                        className="memory-select-btn"
                                        onClick={(e) => {
                                            e.stopPropagation();
                                            handleMemorySelect(res);
                                        }}
                                    >
                                        Open in Chat
                                    </button>
                                </div>
                            ))}
                        </div>
                    </div>
                </div>
            )}
        </div>
    );
};



export default ChatAI;
