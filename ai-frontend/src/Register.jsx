import React, { useEffect, useState } from 'react';
import './Register.css';
import { useLocation, useNavigate } from 'react-router-dom';

const Register = () => {
    const navigate = useNavigate();
    const location = useLocation();
    const [formData, setFormData] = useState({
        username: '',
        email: '',
        password: ''
    });



    const handleChange = (e) => {
        setFormData({ ...formData, [e.target.name]: e.target.value });
    };
    const handleSubmit = async (e) => {
        e.preventDefault();
        const passwordRegex = /^(?=.*[A-Z])(?=.*[!@#$%^&*])(?=.{8,})/;

        if (!passwordRegex.test(formData.password)) {
            alert("⚠️ Password must be 8+ chars, 1 uppercase, and 1 special char.");
            return;
        }
        try {
            const response = await fetch("http://127.0.0.1:8000/register", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(formData),
            });

            const data = await response.json();

            if (response.ok) {
                localStorage.setItem("userToken", "active_session_123");
                localStorage.setItem("userName", formData.username);
                navigate("/?registered=true");
            } else {
                // const errorMsg = data.detail && typeof data.detail === 'string'
                //     ? data.detail
                //     : JSON.stringify(data.detail);

                // alert(`❌ Registration Failed: ${errorMsg}`);
                const errorMsg = data.detail || "Registration failed.";
                alert(`❌ System Error: ${errorMsg}`);
            }
        } catch (err) {
            alert("❌ Connection failed: Neural Link offline.");
        }


    };

    return (
        <div className="auth-container">
            <div className="auth-card">
                <div className="auth-header">
                    <div className="status-dot" style={{ backgroundColor: '#ef4444', boxShadow: '0 0 10px #ef4444' }}></div>
                    <h2 className="auth-title">Create AI Account</h2>
                </div>
                <div className="auth-warning">
                    System Message: Authentication Required for Neural Link.
                </div>
                <form className="auth-form" onSubmit={handleSubmit}>
                    <div className="input-group">
                        <label>Username</label>
                        <input
                            type="text"
                            name="username"
                            className="auth-input"
                            placeholder="Enter username"
                            onChange={handleChange}
                            required
                        />
                    </div>

                    <div className="input-group">
                        <label>Email Address</label>
                        <input
                            type="email"
                            name="email"
                            className="auth-input"
                            placeholder="name@infra.ai"
                            onChange={handleChange}
                            required
                        />
                    </div>

                    <div className="input-group">
                        <label>Password</label>
                        <input
                            type="password"
                            name="password"
                            className="auth-input"
                            placeholder="••••••••"
                            onChange={handleChange}
                            required
                        />
                    </div>

                    <button type="submit" className="submit-btn">
                        Initialize Access
                    </button>
                </form>

                <div className="auth-footer">
                    <p>Already have an account? <span
                        className="link-text"
                        onClick={() => navigate('/login')}
                        style={{ cursor: 'pointer' }}
                    >
                        Login
                    </span></p>
                </div>
            </div>
        </div>
    );
};

export default Register;
