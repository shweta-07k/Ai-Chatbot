import React, { useEffect, useRef, useState } from "react";
import { GoogleLogin } from "@react-oauth/google";

export default function GoogleSignInButton({ mode = "login", disabled, onCredential, onFail }) {
  const containerRef = useRef(null);
  const [width, setWidth] = useState(0);

  useEffect(() => {
    const measure = () => {
      if (containerRef.current) {
        setWidth(containerRef.current.offsetWidth || 320);
      }
    };
    measure();
    window.addEventListener("resize", measure);
    return () => window.removeEventListener("resize", measure);
  }, []);

  return (
    <div ref={containerRef} className="google-btn-container">
      {width > 0 && (
        <GoogleLogin
          onSuccess={(response) => {
            if (response?.credential) {
              onCredential(response.credential);
            } else {
              onFail("Google sign-in did not complete. Please try again.");
            }
          }}
          onError={() => onFail("Google sign-in was cancelled. Please try again or use email.")}
          theme="outline"
          shape="rectangular"
          size="large"
          text={mode === "login" ? "signin_with" : "signup_with"}
          width={width}
          useOneTap={false}
        />
      )}
    </div>
  );
}
