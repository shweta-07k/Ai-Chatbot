export function friendlyError(detail, fallback = "Something went wrong. Please try again.") {
  if (!detail) return fallback;

  const text = typeof detail === "string"
    ? detail
    : Array.isArray(detail)
      ? detail.map((item) => item?.msg || item).join(" ")
      : detail?.message || String(detail);

  const map = {
    "User not found": "We couldn't find an account with that email. Please sign up first.",
    "Invalid password": "That password doesn't look right. Please try again.",
    "User email already exists.": "An account with this email already exists. Please sign in instead.",
    "Invalid or expired token": "Your session has expired. Please sign in again.",
    "Not authenticated": "Please sign in first to continue.",
    Unauthorized: "Please sign in first to continue.",
    "Attachment ingest failed": "We couldn't read that file. Please try PDF or plain text (.txt) instead.",
    "python-docx": "Word files aren't supported on the server right now. Please save as PDF or .txt and try again.",
    "python-pptx": "PowerPoint files aren't supported right now. Please export as PDF and try again.",
  };

  for (const [key, value] of Object.entries(map)) {
    if (text.toLowerCase().includes(key.toLowerCase())) return value;
  }

  if (text.includes("**") && text.includes("upload")) return text.replace(/\*\*/g, "");

  return text;
}

export function friendlyChatError(err) {
  if (err?.name === "AbortError") {
    return "⏳ That took a little too long. Please try again in a moment.";
  }
  if (String(err?.message || "").includes("401")) {
    return "🔐 Please sign in first so I can help you with that.";
  }

  const msg = err?.message || "";
  if (/docx|word file|\.docx/i.test(msg)) {
    return "📄 I couldn't read that Word file. Please save it as **PDF** or **.txt** and upload again — or paste the text here.";
  }
  if (/pptx|powerpoint/i.test(msg)) {
    return "📊 PowerPoint upload didn't work. Please export as **PDF** or upload key slides as images.";
  }
  if (/ingest failed|attachment/i.test(msg)) {
    return friendlyError(msg, "📎 That file couldn't be processed. Try PDF, TXT, or PNG/JPG instead.");
  }

  return friendlyError(msg, "😔 I couldn't reach the server. Please check your connection and try again.");
}

export function friendlyUploadError(detail) {
  const text = friendlyError(detail);
  return text.startsWith("📄") || text.startsWith("😔") ? text : `📎 ${text}`;
}
