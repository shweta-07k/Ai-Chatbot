export const API_URL = process.env.REACT_APP_API_URL || "http://127.0.0.1:8000";
export const GOOGLE_CLIENT_ID = process.env.REACT_APP_GOOGLE_CLIENT_ID || "";

export const GUEST_MESSAGE_LIMIT = 3;

export const INITIAL_AI_MESSAGE = {
  role: "ai",
  text: "Hello! I'm your AI assistant. Ask me anything.",
};

export async function loadGoogleClientId() {
  if (GOOGLE_CLIENT_ID) return GOOGLE_CLIENT_ID;
  try {
    const res = await fetch(`${API_URL}/config/public`);
    if (!res.ok) return "";
    const data = await res.json();
    return data?.google_client_id || "";
  } catch {
    return "";
  }
}
