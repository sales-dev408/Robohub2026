import axios from "axios";

// The desktop build does not set REACT_APP_BACKEND_URL; it falls back to
// the local Python backend that Electron starts automatically.
const BACKEND_URL = process.env.REACT_APP_BACKEND_URL || "http://localhost:54114";
const API = `${BACKEND_URL}/api`;

export const api = axios.create({ baseURL: API });

api.interceptors.request.use((config) => {
  const token = localStorage.getItem("rh_token");
  if (token) {
    config.headers.Authorization = `Bearer ${token}`;
  }
  return config;
});

export const BACKEND_API = API;

// Fire-and-forget ping to wake the Render free-tier backend on page load.
api.get("/").catch(() => {});

export function formatApiErrorDetail(detail) {
  if (detail == null) return "Something went wrong. Please try again.";
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail))
    return detail
      .map((e) => (e && typeof e.msg === "string" ? e.msg : JSON.stringify(e)))
      .filter(Boolean)
      .join(" ");
  if (detail && typeof detail.msg === "string") return detail.msg;
  return String(detail);
}
