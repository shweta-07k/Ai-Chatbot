import React, { useEffect, useState } from "react";
import Modal from "../ui/Modal";
import { useAuth } from "../../context/AuthContext";
import { friendlyError } from "../../utils/friendlyErrors";
import { validateAuthForm } from "../../utils/authValidation";
import GoogleSignInButton from "./GoogleSignInButton";

const EMPTY_FORM = { username: "", email: "", password: "" };

export default function AuthModal({ open, onClose, initialMode = "login", onSuccess, googleEnabled = false }) {
  const { login, register, loginWithGoogle } = useAuth();
  const [mode, setMode] = useState(initialMode);
  const [form, setForm] = useState(EMPTY_FORM);
  const [loading, setLoading] = useState(false);
  const [formError, setFormError] = useState("");
  const [fieldErrors, setFieldErrors] = useState({});

  const showGoogle = googleEnabled;

  useEffect(() => {
    if (open) {
      setMode(initialMode);
      setForm(EMPTY_FORM);
      setFormError("");
      setFieldErrors({});
    }
  }, [open, initialMode]);

  const handleChange = (e) => {
    const { name, value } = e.target;
    setForm((prev) => ({ ...prev, [name]: value }));
    setFieldErrors((prev) => ({ ...prev, [name]: "" }));
    setFormError("");
  };

  const runValidation = () => {
    const result = validateAuthForm(mode, form);
    setFieldErrors(result.fieldErrors);
    setFormError(result.formError);
    return result.valid;
  };

  const handleSubmit = async (e) => {
    e.preventDefault();
    if (!runValidation()) return;

    setLoading(true);
    setFormError("");
    try {
      const result = mode === "login"
        ? await login(form.email.trim(), form.password)
        : await register(form.username.trim(), form.email.trim(), form.password);
      onSuccess?.(mode, result);
      onClose();
    } catch (err) {
      setFormError(friendlyError(err.message));
    } finally {
      setLoading(false);
    }
  };

  const handleGoogleCredential = async (credential) => {
    setLoading(true);
    setFormError("");
    try {
      const result = await loginWithGoogle({ credential });
      onSuccess?.("google", result);
      onClose();
    } catch (err) {
      setFormError(friendlyError(err.message));
    } finally {
      setLoading(false);
    }
  };

  const switchMode = (next) => {
    setMode(next);
    setFormError("");
    setFieldErrors({});
  };

  const renderField = (name, label, type = "text", placeholder, autoComplete, className = "") => (
    <label className={`auth-field ${className} ${fieldErrors[name] ? "has-error" : ""}`.trim()}>
      <span>{label}</span>
      <input
        name={name}
        type={type}
        placeholder={placeholder}
        value={form[name]}
        onChange={handleChange}
        autoComplete={autoComplete}
        aria-invalid={Boolean(fieldErrors[name])}
      />
      {fieldErrors[name] && <em className="field-error">{fieldErrors[name]}</em>}
    </label>
  );

  return (
    <Modal open={open} onClose={onClose} className="auth-modal" labelledBy="auth-modal-title">
      <button type="button" className="modal-close" onClick={onClose} aria-label="Close">×</button>

      <div className="auth-modal-header auth-modal-header-compact">
        <h2 id="auth-modal-title">{mode === "login" ? "Sign in" : "Sign up"}</h2>
        <p>{mode === "login" ? "Access chat, uploads, and history." : "Create an account with email or Google."}</p>
      </div>

      <div className="auth-tabs">
        <button type="button" className={mode === "login" ? "active" : ""} onClick={() => switchMode("login")}>
          Sign in
        </button>
        <button type="button" className={mode === "register" ? "active" : ""} onClick={() => switchMode("register")}>
          Sign up
        </button>
      </div>

      {showGoogle && (
        <div className="google-auth-wrap">
          <GoogleSignInButton
            mode={mode}
            disabled={loading}
            onCredential={handleGoogleCredential}
            onFail={setFormError}
          />
          <div className="divider"><span>or use email</span></div>
        </div>
      )}

      <form className={`auth-form ${mode === "register" ? "auth-form-grid" : ""}`} onSubmit={handleSubmit} noValidate>
        {mode === "register" && (
          <>
            {renderField("username", "Name", "text", "Your name", "name", "auth-field-half")}
            {renderField("email", "Email", "email", "you@email.com", "email", "auth-field-half")}
            {renderField("password", "Password", "password", "Min 8 chars", "new-password", "auth-field-full")}
            <p className="password-hints-inline auth-field-full">
              <span className={form.password.length >= 8 ? "ok" : ""}>8+ chars</span>
              <span className={/[A-Z]/.test(form.password) ? "ok" : ""}>Uppercase</span>
              <span className={/[^A-Za-z0-9]/.test(form.password) ? "ok" : ""}>Symbol</span>
            </p>
          </>
        )}
        {mode === "login" && (
          <>
            {renderField("email", "Email", "email", "you@email.com", "email")}
            {renderField("password", "Password", "password", "Your password", "current-password")}
          </>
        )}

        {formError && <div className="form-error auth-field-full" role="alert">{formError}</div>}

        <button type="submit" className="btn btn-primary btn-full auth-field-full" disabled={loading}>
          {loading ? "Please wait..." : mode === "login" ? "Sign in" : "Sign up"}
        </button>
      </form>
    </Modal>
  );
}
