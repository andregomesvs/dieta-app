// ============================================================
// Configuração do Firebase (frontend)
// Firebase Console -> Configurações do projeto -> Seus apps -> SDK
// Cole aqui os valores do MESMO projeto usado na agenda consolidada.
// ============================================================
export const firebaseConfig = {
  apiKey: "AIzaSyAmy89a5r8kHxLxussQE4AYUgLcCj9JFQw",
  authDomain: "calendar-190c7.firebaseapp.com",
  projectId: "calendar-190c7",
  storageBucket: "calendar-190c7.firebasestorage.app",
  messagingSenderId: "41890555797",
  appId: "1:41890555797:web:e1727dd5aa628bff0ab89c",
  measurementId: "G-09SRK20JTZ",
};

// Base da API do backend. Vazio = mesma origem (o Flask serve o frontend),
// funciona tanto em localhost:5000 quanto no domínio do Render.
export const API_BASE = "";
