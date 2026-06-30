export function formatChatTranscript(messages, { includeWelcome = false } = {}) {
  if (!Array.isArray(messages) || !messages.length) return "";

  return messages
    .filter((msg) => {
      if (!includeWelcome && msg.role === "ai" && msg.isWelcome) return false;
      return Boolean((msg.text || "").trim());
    })
    .map((msg) => {
      const label = msg.role === "user" ? "You" : "Nova AI";
      return `${label}:\n${msg.text.trim()}`;
    })
    .join("\n\n---\n\n");
}

export function stripAttachmentSuffix(text) {
  return (text || "").replace(/\n\n\[Attached:[^\]]+\]\s*$/, "").trim();
}
