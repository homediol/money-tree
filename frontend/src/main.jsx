import React from 'react';
import ReactDOM from 'react-dom/client';
import { BrowserRouter, Routes, Route, Navigate } from 'react-router-dom';
import Dashboard   from './pages/Dashboard.jsx';
import BotControl  from './pages/BotControl.jsx';
import Login       from './pages/Login.jsx';
import Predictions from './pages/Predictions.jsx';
import MLDashboard from './pages/MLDashboard.jsx';
import { isAuthenticated } from './auth.js';
import './styles/index.css';

function PrivateRoute({ children }) {
  return isAuthenticated() ? children : <Navigate to="/login" replace />;
}

ReactDOM.createRoot(document.getElementById('root')).render(
  <React.StrictMode>
    <BrowserRouter>
      <Routes>
        <Route path="/login"       element={<Login />} />
        <Route path="/"            element={<PrivateRoute><Dashboard /></PrivateRoute>} />
        <Route path="/bot"         element={<PrivateRoute><BotControl /></PrivateRoute>} />
        <Route path="/predictions" element={<PrivateRoute><Predictions /></PrivateRoute>} />
        <Route path="/ml-dashboard" element={<PrivateRoute><MLDashboard /></PrivateRoute>} />
        <Route path="*"            element={<Navigate to="/" replace />} />
      </Routes>
    </BrowserRouter>
  </React.StrictMode>,
);
