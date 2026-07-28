# Minha Dieta — v1

App gratuito para gestão de dieta de perda de peso. Backend **Python (Flask)**,
frontend **HTML/JS**, **Firebase** (Auth + Firestore) e **Google Gemini** para
ler o PDF de bioimpedância e montar a dieta.

## O que a v1 faz

1. **Cadastro / login** com Firebase Auth (e-mail e senha).
2. **Importar bioimpedância**: você anexa o PDF do exame e a IA lê e preenche
   automaticamente o formulário de dados (peso, % gordura, massa magra, TMB...).
3. **Dados pessoais**: formulário editável, salvo no Firestore.
4. **Objetivo + rotina**: objetivo, prazo (mensal/trimestral/anual), rotina de
   treino e alimentar com horários.
5. **Gerar dieta**: a IA monta um plano com refeições, horários, porções,
   calorias e macros — salvo no histórico do usuário.

> Fotos de corpo ficaram para a v2 (conforme combinado).

## Estrutura

```
dieta-app/
├─ backend/
│  ├─ app.py                # Flask + Claude + Firestore
│  ├─ requirements.txt
│  └─ .env.example
├─ frontend/
│  ├─ index.html            # login/cadastro
│  ├─ app.html              # dashboard
│  └─ static/
│     ├─ css/style.css
│     └─ js/firebase-config.js
├─ firestore.rules
└─ README.md
```

## Passo a passo para rodar

### 1. Firebase (reaproveite o projeto da agenda consolidada, se quiser)

1. No [Firebase Console](https://console.firebase.google.com/), crie/abra o projeto.
2. **Authentication → Sign-in method → Email/senha → Ativar.**
3. **Firestore Database → Criar banco** (modo produção).
4. Cole o conteúdo de `firestore.rules` em **Firestore → Regras** e publique.
5. **Configurações do projeto → Seus apps (Web)**: copie o objeto de config e
   cole em `frontend/static/js/firebase-config.js`.
6. **Configurações → Contas de serviço → Gerar nova chave privada**: salve o
   JSON como `backend/firebase-service-account.json`.

### 2. Google Gemini

1. Em [Google AI Studio](https://aistudio.google.com/app/apikey) crie uma API Key
   (é gratuita; use a mesma conta Google do Firebase, se quiser).
2. Copie `backend/.env.example` para `backend/.env` e preencha `GEMINI_API_KEY`.

### 3. Backend

```bash
cd backend
python -m venv .venv
# Windows:  .venv\Scripts\activate
# Linux/Mac: source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # e edite com suas chaves
python app.py
```

O app sobe em `http://localhost:5000`. O próprio Flask serve o frontend:
abra **http://localhost:5000** no navegador.

## Modelo de IA e custo

- O modelo é configurável em `.env` (`GEMINI_MODEL`). O padrão `gemini-1.5-flash`
  é rápido e está no tier gratuito do Gemini.
- Firebase Auth e Firestore têm plano gratuito (Spark) suficiente para uso
  pessoal. O Gemini tem um tier gratuito com limite diário de requisições —
  suficiente para uso pessoal.

## Dados no Firestore

```
usuarios/{uid}                     -> dados pessoais + últimas respostas
usuarios/{uid}/dietas/{id}         -> cada dieta gerada (com data)
```

## Aviso

As dietas são **sugestões geradas por IA** e não substituem a orientação de um
nutricionista ou médico. Para perda de peso saudável, procure acompanhamento
profissional.

## Próximos passos (v2)

- Upload de fotos de corpo (frente/costas/lado) no Firebase Storage.
- Acompanhamento de progresso (peso ao longo do tempo, gráficos).
- Ajuste da dieta a partir do progresso.
- Lista de compras a partir da dieta.
