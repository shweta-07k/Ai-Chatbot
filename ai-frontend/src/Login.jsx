import React, { useState } from 'react';
import './Register.css';
import { useNavigate } from 'react-router-dom';

const Login = () => {
    const navigate = useNavigate();

    const [formData, setFormData] = useState({
        email: '',
        password: ''
    });


    const handleSubmit = async (e) => {
        e.preventDefault();

        try {
            const response = await fetch("http://127.0.0.1:8000/login", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(formData),
            });

            const data = await response.json();
            console.log("LOGIN DATA:", data);
            if (response.ok) {
                // ✅ Save session
                localStorage.setItem("token", data.access_token);
                localStorage.setItem("userEmail", data.email);
                localStorage.setItem("username", data.username);

                console.log("LOGIN RESPONSE:", data);
                console.log("TOKEN SAVED:", data.access_token);
                navigate('/?login=true'); // go to home
            } else {
                alert(`❌ Login Failed: ${data.detail}`);
            }

        } catch (err) {
            alert("❌ Connection failed: Server offline.");
        }
    };


    const handleChange = (e) => {
        setFormData({ ...formData, [e.target.name]: e.target.value });
    };


    return (
        <div className="auth-container">
            <div className="auth-card">
                <div className="auth-header">
                    <div className="status-dot" style={{ backgroundColor: '#10b981', boxShadow: '0 0 10px #10b981' }}></div>
                    <h2 className="auth-title">Access AI System</h2>
                </div>

                <div className="auth-warning">
                    System Message: Login required to continue.
                </div>

                <form className="auth-form" onSubmit={handleSubmit}>
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
                        Authenticate
                    </button>
                </form>

                <div className="auth-footer">
                    <p>
                        New user?{" "}
                        <span
                            className="link-text"
                            onClick={() => navigate('/register')}
                            style={{ cursor: 'pointer' }}
                        >
                            Register
                        </span>
                    </p>
                </div>
            </div>
        </div>
    );
};

export default Login;