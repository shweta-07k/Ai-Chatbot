import React, { useEffect, useState } from "react";
import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";
import { GoogleOAuthProvider } from "@react-oauth/google";
import { API_URL, GOOGLE_CLIENT_ID } from "./config";
import { ThemeProvider } from "./context/ThemeContext";
import { AuthProvider } from "./context/AuthContext";
import ChatApp from "./pages/ChatApp";
import LoginRedirect from "./pages/LoginRedirect";
import RegisterRedirect from "./pages/RegisterRedirect";

function AppRoutes({ googleEnabled }) {
  return (
    <ThemeProvider>
      <AuthProvider>
        <BrowserRouter>
          <Routes>
            <Route path="/" element={<ChatApp googleEnabled={googleEnabled} />} />
            <Route path="/login" element={<LoginRedirect />} />
            <Route path="/register" element={<RegisterRedirect />} />
            <Route path="*" element={<Navigate to="/" replace />} />
          </Routes>
        </BrowserRouter>
      </AuthProvider>
    </ThemeProvider>
  );
}

function App() {
  const [googleClientId, setGoogleClientId] = useState(GOOGLE_CLIENT_ID);

  useEffect(() => {
    if (googleClientId) return;
    fetch(`${API_URL}/config/public`)
      .then((res) => (res.ok ? res.json() : {}))
      .then((data) => {
        if (data?.google_client_id) {
          setGoogleClientId(data.google_client_id);
        }
      })
      .catch(() => {});
  }, [googleClientId]);

  const routes = <AppRoutes googleEnabled={Boolean(googleClientId)} />;

  if (googleClientId) {
    return (
      <GoogleOAuthProvider clientId={googleClientId}>
        {routes}
      </GoogleOAuthProvider>
    );
  }

  return routes;
}

export default App;
