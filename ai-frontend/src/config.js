export const API_URL = normalizeApiUrl(process.env.REACT_APP_API_URL);
export const GOOGLE_CLIENT_ID = process.env.REACT_APP_GOOGLE_CLIENT_ID || "";

function normalizeApiUrl(raw) {
  const value = (raw || "").trim();
  if (!value) return "http://127.0.0.1:8000";
  if (/^https?:\/\//i.test(value)) return value.replace(/\/+$/, "");
  return `https://${value.replace(/\/+$/, "")}`;
}

export const GUEST_MESSAGE_LIMIT = 3;

export const INITIAL_AI_MESSAGE = {
  role: "ai",
  text: "Hello! I'm your AI assistant. Ask me anything.",
  isWelcome: true,
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
