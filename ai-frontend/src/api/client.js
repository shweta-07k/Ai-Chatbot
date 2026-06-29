import { API_URL } from "../config";
import { friendlyError } from "../utils/friendlyErrors";

export function getAuthToken() {
  return localStorage.getItem("token") || localStorage.getItem("userToken");
}

export async function apiRequest(path, { method = "GET", body, token, headers = {} } = {}) {
  const finalHeaders = { ...headers };
  if (token) finalHeaders.Authorization = `Bearer ${token}`;

  let payload = body;
  if (body && !(body instanceof FormData)) {
    finalHeaders["Content-Type"] = "application/json";
    payload = JSON.stringify(body);
  }

  const res = await fetch(`${API_URL}${path}`, {
    method,
    headers: finalHeaders,
    body: payload,
  });

  let data = {};
  try {
    data = await res.json();
  } catch {
    data = {};
  }

  if (!res.ok) {
    throw new Error(friendlyError(data?.detail, `Request failed (${res.status})`));
  }

  return data;
}
