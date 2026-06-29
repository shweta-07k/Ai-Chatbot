import React, { createContext, useCallback, useContext, useMemo, useState } from "react";
import { apiRequest, getAuthToken } from "../api/client";
import { INITIAL_AI_MESSAGE } from "../config";

const AuthContext = createContext(null);

function sessionStorageKey(email) {
  return email ? `chatSessionId-${email}` : "chatSessionId";
}

function createSessionId() {
  return `session_${Date.now()}_${Math.random().toString(36).slice(2, 11)}`;
}

export function AuthProvider({ children }) {
  const [user, setUser] = useState(() => {
    const email = localStorage.getItem("userEmail");
    const username = localStorage.getItem("username");
    return email ? { email, username: username || "User" } : null;
  });

  const persistAuth = useCallback((payload) => {
    localStorage.setItem("token", payload.access_token);
    localStorage.setItem("userToken", payload.access_token);
    localStorage.setItem("userEmail", payload.email);
    localStorage.setItem("username", payload.username);
    setUser({
      email: payload.email,
      username: payload.username,
      auth_provider: payload.auth_provider || "email",
      avatar_url: payload.avatar_url,
    });
  }, []);

  const startFreshSession = useCallback((email) => {
    const key = sessionStorageKey(email);
    const priorSessionId = localStorage.getItem(key);
    if (priorSessionId) {
      localStorage.removeItem(`chatMessages-${priorSessionId}`);
    }
    const freshSessionId = createSessionId();
    localStorage.setItem(key, freshSessionId);
    localStorage.setItem(`chatMessages-${freshSessionId}`, JSON.stringify([INITIAL_AI_MESSAGE]));
    localStorage.setItem("queryCount", "0");
    return freshSessionId;
  }, []);

  const login = useCallback(async (email, password) => {
    const data = await apiRequest("/login", {
      method: "POST",
      body: { email, password },
    });
    persistAuth(data);
    const sessionId = startFreshSession(data.email);
    return { ...data, sessionId };
  }, [persistAuth, startFreshSession]);

  const register = useCallback(async (username, email, password) => {
    const data = await apiRequest("/register", {
      method: "POST",
      body: { username, email, password },
    });
    persistAuth(data);
    const sessionId = startFreshSession(data.email);
    return { ...data, sessionId };
  }, [persistAuth, startFreshSession]);

  const loginWithGoogle = useCallback(async ({ credential, access_token: accessToken }) => {
    const data = await apiRequest("/auth/google", {
      method: "POST",
      body: { credential, access_token: accessToken },
    });
    persistAuth(data);
    const sessionId = startFreshSession(data.email);
    return { ...data, sessionId };
  }, [persistAuth, startFreshSession]);

  const logout = useCallback(() => {
    const email = localStorage.getItem("userEmail");
    const key = sessionStorageKey(email);
    const priorSessionId = localStorage.getItem(key);

    localStorage.removeItem("token");
    localStorage.removeItem("userToken");
    localStorage.removeItem("username");
    localStorage.removeItem("userEmail");
    localStorage.removeItem("queryCount");
    localStorage.removeItem(key);
    localStorage.removeItem("chatSessionId");
    if (priorSessionId) {
      localStorage.removeItem(`chatMessages-${priorSessionId}`);
    }

    const guestSessionId = createSessionId();
    localStorage.setItem("chatSessionId", guestSessionId);
    localStorage.setItem(`chatMessages-${guestSessionId}`, JSON.stringify([INITIAL_AI_MESSAGE]));

    setUser(null);
    return guestSessionId;
  }, []);

  const refreshProfile = useCallback(async () => {
    const token = getAuthToken();
    if (!token) return null;
    const profile = await apiRequest("/me", { token });
    localStorage.setItem("username", profile.username);
    setUser((prev) => ({ ...prev, ...profile }));
    return profile;
  }, []);

  const updateProfile = useCallback(async (payload) => {
    const token = getAuthToken();
    const data = await apiRequest("/me", { method: "PUT", token, body: payload });
    if (data?.user?.username) {
      localStorage.setItem("username", data.user.username);
      setUser((prev) => ({ ...prev, ...data.user }));
    }
    return data;
  }, []);

  const value = useMemo(
    () => ({
      user,
      isAuthenticated: Boolean(user?.email && getAuthToken()),
      login,
      register,
      loginWithGoogle,
      logout,
      refreshProfile,
      updateProfile,
      startFreshSession,
    }),
    [user, login, register, loginWithGoogle, logout, refreshProfile, updateProfile, startFreshSession]
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth() {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used within AuthProvider");
  return ctx;
}
