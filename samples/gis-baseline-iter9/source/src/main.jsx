import { AuthGate, installAuthFetch } from './auth.jsx'
import 'leaflet/dist/leaflet.css'
import React from 'react'
import ReactDOM from 'react-dom/client'
import App from './App.jsx'
import './visual.css'

installAuthFetch()

ReactDOM.createRoot(document.getElementById('root')).render(
  <React.StrictMode>
    <AuthGate><App /></AuthGate>
  </React.StrictMode>,
)
