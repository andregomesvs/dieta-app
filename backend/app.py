"""
Dieta App - Backend Flask
--------------------------
Gerencia dados pessoais, leitura de PDF de bioimpedância via Claude API
e geração de dieta personalizada. Persistência em Firebase Firestore.

Rotas:
  GET  /                      -> página de login
  GET  /app                   -> dashboard (protegido no front)
  POST /api/analyze-pdf       -> recebe PDF, Claude extrai dados de bioimpedância
  GET  /api/profile           -> retorna perfil do usuário
  POST /api/profile           -> salva/atualiza perfil
  POST /api/generate-diet     -> gera dieta com Claude e salva
  GET  /api/diets             -> lista dietas geradas do usuário

Todas as rotas /api/* (exceto analyze-pdf sem persistência) exigem
o header Authorization: Bearer <Firebase ID Token>.
"""

import os
import json
import base64
import datetime
from functools import wraps

from dotenv import load_dotenv
from flask import Flask, request, jsonify, send_from_directory, g
from flask_cors import CORS

import firebase_admin
from firebase_admin import credentials, auth as fb_auth, firestore

import anthropic

load_dotenv()

# ---------------------------------------------------------------------------
# Configuração
# ---------------------------------------------------------------------------
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-5")
CRED_PATH = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "./firebase-service-account.json")

FRONTEND_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "frontend")
)

# ---------------------------------------------------------------------------
# Inicialização Firebase Admin
# ---------------------------------------------------------------------------
_db = None


def init_firebase():
    global _db
    if firebase_admin._apps:
        _db = firestore.client()
        return
    if os.path.exists(CRED_PATH):
        cred = credentials.Certificate(CRED_PATH)
        firebase_admin.initialize_app(cred)
    else:
        # Em ambientes gerenciados (Cloud Run, etc.) usa credencial padrão
        firebase_admin.initialize_app()
    _db = firestore.client()


def db():
    if _db is None:
        init_firebase()
    return _db


# ---------------------------------------------------------------------------
# Cliente Claude
# ---------------------------------------------------------------------------
def claude():
    if not ANTHROPIC_API_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY não configurada no .env")
    return anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)


# ---------------------------------------------------------------------------
# App Flask
# ---------------------------------------------------------------------------
app = Flask(__name__, static_folder=None)
CORS(app)

# Inicializa o Firebase já na importação do módulo — necessário quando o app
# roda sob gunicorn (Render), onde o bloco __main__ não é executado.
try:
    init_firebase()
except Exception as _e:  # noqa: BLE001
    print("Aviso: Firebase não inicializado na importação:", _e)


def require_auth(f):
    """Valida o Firebase ID Token enviado no header Authorization."""

    @wraps(f)
    def wrapper(*args, **kwargs):
        header = request.headers.get("Authorization", "")
        if not header.startswith("Bearer "):
            return jsonify({"error": "Token ausente"}), 401
        token = header.split(" ", 1)[1]
        if not firebase_admin._apps:
            init_firebase()
        try:
            decoded = fb_auth.verify_id_token(token)
            g.uid = decoded["uid"]
            g.email = decoded.get("email")
        except Exception as e:  # noqa: BLE001
            return jsonify({"error": f"Token inválido: {e}"}), 401
        return f(*args, **kwargs)

    return wrapper


# ---------------------------------------------------------------------------
# Páginas
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    return send_from_directory(FRONTEND_DIR, "index.html")


@app.route("/app")
def dashboard():
    return send_from_directory(FRONTEND_DIR, "app.html")


@app.route("/static/<path:path>")
def static_files(path):
    return send_from_directory(os.path.join(FRONTEND_DIR, "static"), path)


@app.route("/health")
def health():
    return jsonify({"status": "ok", "model": ANTHROPIC_MODEL})


# ---------------------------------------------------------------------------
# API: análise de PDF de bioimpedância
# ---------------------------------------------------------------------------
PDF_EXTRACT_PROMPT = """Você é um assistente que extrai dados de exames de \
bioimpedância. Analise o PDF anexado e devolva APENAS um objeto JSON válido \
(sem markdown, sem explicação) com as chaves abaixo. Use null quando o dado \
não estiver presente no exame.

{
  "nome": string|null,
  "sexo": "masculino"|"feminino"|null,
  "idade": number|null,
  "altura_cm": number|null,
  "peso_kg": number|null,
  "imc": number|null,
  "percentual_gordura": number|null,
  "massa_magra_kg": number|null,
  "massa_muscular_kg": number|null,
  "gordura_visceral": number|null,
  "agua_corporal_percent": number|null,
  "taxa_metabolica_basal_kcal": number|null,
  "observacoes": string|null
}

Responda somente com o JSON."""


@app.route("/api/analyze-pdf", methods=["POST"])
@require_auth
def analyze_pdf():
    """Recebe um PDF (multipart 'file' ou JSON base64) e extrai os dados."""
    pdf_b64 = None

    if "file" in request.files:
        pdf_b64 = base64.standard_b64encode(request.files["file"].read()).decode()
    else:
        data = request.get_json(silent=True) or {}
        pdf_b64 = data.get("pdf_base64")

    if not pdf_b64:
        return jsonify({"error": "Nenhum PDF enviado"}), 400

    try:
        msg = claude().messages.create(
            model=ANTHROPIC_MODEL,
            max_tokens=1500,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "document",
                            "source": {
                                "type": "base64",
                                "media_type": "application/pdf",
                                "data": pdf_b64,
                            },
                        },
                        {"type": "text", "text": PDF_EXTRACT_PROMPT},
                    ],
                }
            ],
        )
        raw = msg.content[0].text.strip()
        parsed = _safe_json(raw)
        return jsonify({"data": parsed, "raw": raw})
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------------
# API: perfil do usuário
# ---------------------------------------------------------------------------
@app.route("/api/profile", methods=["GET"])
@require_auth
def get_profile():
    doc = db().collection("usuarios").document(g.uid).get()
    return jsonify(doc.to_dict() or {})


@app.route("/api/profile", methods=["POST"])
@require_auth
def save_profile():
    data = request.get_json(silent=True) or {}
    data["email"] = g.email
    data["atualizado_em"] = datetime.datetime.utcnow().isoformat()
    db().collection("usuarios").document(g.uid).set(data, merge=True)
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# API: geração da dieta
# ---------------------------------------------------------------------------
DIET_SYSTEM = """Você é um nutricionista virtual. Monte um plano alimentar \
personalizado, prático e realista, respeitando os horários, rotina de treino \
e preferências informadas. Priorize alimentos acessíveis no Brasil. \
Estruture em refeições com horário, itens e porções, e some as calorias e \
macros por refeição e no total diário.

Devolva APENAS um JSON válido (sem markdown) neste formato:
{
  "resumo": string,
  "calorias_alvo_dia": number,
  "macros_dia": {"proteina_g": number, "carbo_g": number, "gordura_g": number},
  "refeicoes": [
    {
      "nome": string,
      "horario": string,
      "itens": [{"alimento": string, "porcao": string, "kcal": number}],
      "kcal_total": number
    }
  ],
  "observacoes": string,
  "aviso": "Este plano é uma sugestão gerada por IA e não substitui um nutricionista."
}"""


@app.route("/api/generate-diet", methods=["POST"])
@require_auth
def generate_diet():
    data = request.get_json(silent=True) or {}
    perfil = data.get("perfil", {})
    objetivo = data.get("objetivo", "")
    prazo = data.get("prazo", "")  # mensal | trimestral | anual
    rotina_treino = data.get("rotina_treino", "")
    rotina_alimentar = data.get("rotina_alimentar", "")
    restricoes = data.get("restricoes", "")

    user_msg = f"""Dados do usuário:
{json.dumps(perfil, ensure_ascii=False, indent=2)}

Objetivo: {objetivo}
Prazo do objetivo: {prazo}
Rotina de treino (com horários): {rotina_treino}
Rotina alimentar atual e horários: {rotina_alimentar}
Restrições / preferências alimentares: {restricoes or "nenhuma informada"}

Monte a dieta seguindo o formato JSON solicitado."""

    try:
        msg = claude().messages.create(
            model=ANTHROPIC_MODEL,
            max_tokens=3000,
            system=DIET_SYSTEM,
            messages=[{"role": "user", "content": user_msg}],
        )
        raw = msg.content[0].text.strip()
        dieta = _safe_json(raw)

        registro = {
            "objetivo": objetivo,
            "prazo": prazo,
            "dieta": dieta,
            "criado_em": datetime.datetime.utcnow().isoformat(),
        }
        ref = (
            db()
            .collection("usuarios")
            .document(g.uid)
            .collection("dietas")
            .add(registro)
        )
        registro["id"] = ref[1].id
        return jsonify(registro)
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": str(e)}), 500


@app.route("/api/diets", methods=["GET"])
@require_auth
def list_diets():
    docs = (
        db()
        .collection("usuarios")
        .document(g.uid)
        .collection("dietas")
        .order_by("criado_em", direction=firestore.Query.DESCENDING)
        .limit(20)
        .stream()
    )
    out = []
    for d in docs:
        item = d.to_dict()
        item["id"] = d.id
        out.append(item)
    return jsonify(out)


# ---------------------------------------------------------------------------
# Utilidades
# ---------------------------------------------------------------------------
def _safe_json(text):
    """Extrai o primeiro objeto JSON de uma string, tolerando cercas de código."""
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lstrip().startswith("json"):
            text = text.lstrip()[4:]
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1:
        text = text[start : end + 1]
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"_parse_error": True, "conteudo": text}


if __name__ == "__main__":
    init_firebase()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
