const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

export function validateAuthForm(mode, form) {
  const fieldErrors = {};
  let formError = "";

  const email = (form.email || "").trim();
  const password = form.password || "";
  const username = (form.username || "").trim();

  if (!email) {
    fieldErrors.email = "Please enter your email address.";
  } else if (!EMAIL_RE.test(email)) {
    fieldErrors.email = "Please enter a valid email address (example: you@company.com).";
  }

  if (!password) {
    fieldErrors.password = "Please enter your password.";
  } else if (mode === "register") {
    if (password.length < 8) {
      fieldErrors.password = "Password must be at least 8 characters.";
    } else if (!/[A-Z]/.test(password)) {
      fieldErrors.password = "Include at least one uppercase letter (A–Z).";
    } else if (!/[^A-Za-z0-9]/.test(password)) {
      fieldErrors.password = "Include at least one special character (!@#$…).";
    }
  } else if (password.length < 1) {
    fieldErrors.password = "Please enter your password.";
  }

  if (mode === "register") {
    if (!username) {
      fieldErrors.username = "Please enter your display name.";
    } else if (username.length < 2) {
      fieldErrors.username = "Display name must be at least 2 characters.";
    }
  }

  const keys = Object.keys(fieldErrors);
  if (keys.length === 1) {
    formError = fieldErrors[keys[0]];
  } else if (keys.length > 1) {
    formError = "Please fix the highlighted fields below.";
  }

  return { fieldErrors, formError, valid: keys.length === 0 };
}
