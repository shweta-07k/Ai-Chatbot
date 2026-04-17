// import { BrowserRouter as Router, Routes, Route, Navigate } from 'react-router-dom';
import { BrowserRouter, Navigate, Route, Routes } from 'react-router-dom';
import ChatAI from './ChatAI';
import Register from './Register';
import { ToastContainer } from 'react-toastify';
import 'react-toastify/dist/ReactToastify.css';
import Login from './Login';

function App() {
  const isAuthenticated = !!localStorage.getItem("userToken");

  return (
    <BrowserRouter>
      <Routes>

        <Route
          path="/" element={<ChatAI />}
        />
        <Route path="/register" element={<Register />} />
        <Route path="/login" element={<Login />} />
      </Routes>
      <ToastContainer
        position="top-center"
        autoClose={3000}
        hideProgressBar={false}
        newestOnTop={true}
        closeOnClick={true}
        pauseOnHover={true}
        draggable={true}
        theme="dark"
        toastClassName="modern-toast"
        bodyClassName="modern-toast-body"
        progressClassName="modern-toast-progress"
      />
    </BrowserRouter>
  );
}
export default App;