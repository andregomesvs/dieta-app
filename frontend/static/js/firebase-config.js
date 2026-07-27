// ============================================================
// Configuração do Firebase (frontend)
// Firebase Console -> Configurações do projeto -> Seus apps -> SDK
// Cole aqui os valores do MESMO projeto usado na agenda consolidada.
// ============================================================
export const firebaseConfig = {
  apiKey: "SUA_API_KEY",
  authDomain: "seu-projeto.firebaseapp.com",
  projectId: "seu-projeto",
  storageBucket: "seu-projeto.appspot.com",
  messagingSenderId: "000000000000",
  appId: "1:000000000000:web:xxxxxxxxxxxx",
};

// Base da API do backend. Vazio = mesma origem (o Flask serve o frontend),
// funciona tanto em localhost:5000 quanto no domínio do Render.
export const API_BASE = "";
