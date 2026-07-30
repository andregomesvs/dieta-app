"""
Dieta App - Backend Flask
--------------------------
Gerencia dados pessoais, leitura de PDF de bioimpedância via Google Gemini
e geração de dieta personalizada. Persistência em Firebase Firestore.

Rotas:
  GET  /                      -> página de login
  GET  /app                   -> dashboard (protegido no front)
  POST /api/analyze-pdf       -> recebe PDF, Gemini extrai dados de bioimpedância
  GET  /api/profile           -> retorna perfil do usuário
  POST /api/profile           -> salva/atualiza perfil
  POST /api/generate-diet     -> gera dieta com Gemini e salva
  GET  /api/diets             -> lista dietas geradas do usuário

Todas as rotas /api/* (exceto analyze-pdf sem persistência) exigem
o header Authorization: Bearer <Firebase ID Token>.
"""

import os
import re
import json
import base64
import secrets
import datetime
import urllib.parse
import urllib.request
from functools import wraps

try:
    from zoneinfo import ZoneInfo
except ImportError:  # Python < 3.9
    ZoneInfo = None

from dotenv import load_dotenv
from flask import Flask, request, jsonify, send_from_directory, g

import firebase_admin
from firebase_admin import credentials, auth as fb_auth, firestore

import google.generativeai as genai

load_dotenv()

# ---------------------------------------------------------------------------
# Configuração
# ---------------------------------------------------------------------------
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-flash-latest")
CRED_PATH = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "./firebase-service-account.json")
FIREBASE_PROJECT_ID = os.environ.get("FIREBASE_PROJECT_ID", "")
# E-mails com acesso administrativo (somente leitura). Separados por vírgula.
# Sem padrão embutido: defina ADMIN_EMAILS no ambiente (ex.: no Render).
ADMIN_EMAILS = {e.strip().lower() for e in os.environ.get(
    "ADMIN_EMAILS", "").split(",") if e.strip()}
# E-mails de personal trainers credenciados da plataforma (papel 'personal').
PERSONAL_EMAILS = {e.strip().lower() for e in os.environ.get(
    "PERSONAL_EMAILS", "").split(",") if e.strip()}

# Tamanho máximo de upload (10 MB) e tipos aceitos em documentos.
MAX_UPLOAD_BYTES = 10 * 1024 * 1024

# Telegram + agendamento de lembretes
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_BOT_USERNAME = os.environ.get("TELEGRAM_BOT_USERNAME", "minhadietazbot")
CRON_SECRET = os.environ.get("CRON_SECRET", "")
TIMEZONE = os.environ.get("TIMEZONE", "America/Sao_Paulo")
# Tolerância (min): manda a refeição se o horário caiu nos últimos N minutos.
REMINDER_WINDOW_MIN = int(os.environ.get("REMINDER_WINDOW_MIN", "15"))
# Segredo do webhook do Telegram (validado no header). Sem fallback previsível:
# usa a variável própria ou o CRON_SECRET; se ambos vazios, o webhook rejeita tudo.
TELEGRAM_WEBHOOK_SECRET = os.environ.get("TELEGRAM_WEBHOOK_SECRET") or CRON_SECRET or ""
# URL pública do app (o Render fornece RENDER_EXTERNAL_URL automaticamente).
PUBLIC_URL = os.environ.get("PUBLIC_URL") or os.environ.get("RENDER_EXTERNAL_URL", "")

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
        print(f"[firebase] inicializado com service account: {CRED_PATH}")
    else:
        # Sem o arquivo de credencial. Sem ele o Firestore não funciona;
        # deixamos ao menos o project ID explícito para verificar tokens.
        print(
            f"[firebase] AVISO: credencial NÃO encontrada em '{CRED_PATH}'. "
            "Configure o Secret File no Render. Firestore ficará indisponível."
        )
        opts = {"projectId": FIREBASE_PROJECT_ID} if FIREBASE_PROJECT_ID else None
        firebase_admin.initialize_app(options=opts)
    _db = firestore.client()


def db():
    if _db is None:
        init_firebase()
    return _db


# ---------------------------------------------------------------------------
# Cliente Gemini
# ---------------------------------------------------------------------------
_gemini_ready = False


def gemini(system_instruction=None):
    global _gemini_ready
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY não configurada no .env")
    if not _gemini_ready:
        genai.configure(api_key=GEMINI_API_KEY)
        _gemini_ready = True
    return genai.GenerativeModel(
        GEMINI_MODEL,
        system_instruction=system_instruction,
        generation_config={"response_mime_type": "application/json"},
    )


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------
TG_API = "https://api.telegram.org/bot{token}/{method}"


def tg_call(method, params):
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN não configurado")
    url = TG_API.format(token=TELEGRAM_BOT_TOKEN, method=method)
    data = urllib.parse.urlencode(params).encode()
    with urllib.request.urlopen(url, data=data, timeout=20) as r:
        return json.loads(r.read().decode())


def tg_send(chat_id, text, reply_markup=None):
    params = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    }
    if reply_markup is not None:
        params["reply_markup"] = json.dumps(reply_markup)
    return tg_call("sendMessage", params)


def tg_answer_callback(callback_id, text=""):
    try:
        return tg_call("answerCallbackQuery", {"callback_query_id": callback_id, "text": text})
    except Exception:  # noqa: BLE001
        return None


def tg_remove_keyboard(chat_id, message_id):
    try:
        return tg_call("editMessageReplyMarkup", {
            "chat_id": chat_id, "message_id": message_id,
            "reply_markup": json.dumps({"inline_keyboard": []}),
        })
    except Exception:  # noqa: BLE001
        return None


def tg_updates():
    """Retorna os chats que mandaram mensagem recente para o bot."""
    res = tg_call("getUpdates", {})
    chats = {}
    for upd in res.get("result", []):
        msg = upd.get("message") or upd.get("edited_message") or {}
        chat = msg.get("chat")
        if chat:
            nome = " ".join(filter(None, [chat.get("first_name"), chat.get("last_name")])) \
                or chat.get("username") or str(chat["id"])
            chats[str(chat["id"])] = nome
    return [{"chat_id": k, "nome": v} for k, v in chats.items()]


def parse_horario(txt):
    """Extrai (hora, minuto) de strings como '09:00', '9h', '12h30', '7:5'."""
    if not txt:
        return None
    m = re.search(r"(\d{1,2})\s*[:hH]\s*(\d{1,2})?", str(txt))
    if not m:
        return None
    h = int(m.group(1))
    mnt = int(m.group(2)) if m.group(2) else 0
    if 0 <= h <= 23 and 0 <= mnt <= 59:
        return h, mnt
    return None


def now_local():
    if ZoneInfo:
        try:
            return datetime.datetime.now(ZoneInfo(TIMEZONE))
        except Exception:  # noqa: BLE001
            pass
    return datetime.datetime.now()


def _esc(s):
    """Escapa caracteres reservados do HTML do Telegram."""
    return (str(s if s is not None else "")
            .replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def format_meal_message(refeicao):
    """Monta a mensagem de uma refeição no formato do app."""
    nome = _esc(refeicao.get("nome", "Refeição"))
    horario = _esc(refeicao.get("horario", ""))
    kcal = refeicao.get("kcal_total")
    cab = f"🍽 <b>{nome}</b>"
    if horario:
        cab += f" — {horario}"
    if kcal:
        cab += f" · {kcal} kcal"
    linhas = [cab]
    for it in refeicao.get("itens", []):
        alimento = _esc(it.get("alimento", ""))
        porcao = _esc(it.get("porcao", ""))
        ik = it.get("kcal")
        linha = f"• {alimento}"
        if porcao:
            linha += f" — {porcao}"
        if ik:
            linha += f" · {ik} kcal"
        linhas.append(linha)
    return "\n".join(linhas)


# ---------------------------------------------------------------------------
# App Flask
# ---------------------------------------------------------------------------
app = Flask(__name__, static_folder=None)
# App é same-origin (o próprio Flask serve o frontend), então não habilitamos
# CORS global. Limite de tamanho de upload:
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_BYTES


@app.errorhandler(413)
def too_large(_e):
    return jsonify({"error": "Arquivo muito grande (máx. 10 MB)."}), 413


@app.errorhandler(500)
def internal_error(e):
    print("[erro-500]", repr(e))
    return jsonify({"error": "Erro interno. Tente novamente."}), 500


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


def require_admin(f):
    """Como require_auth, mas exige e-mail na lista ADMIN_EMAILS."""

    @wraps(f)
    def wrapper(*args, **kwargs):
        header = request.headers.get("Authorization", "")
        if not header.startswith("Bearer "):
            return jsonify({"error": "Token ausente"}), 401
        if not firebase_admin._apps:
            init_firebase()
        try:
            decoded = fb_auth.verify_id_token(header.split(" ", 1)[1])
            g.uid = decoded["uid"]
            g.email = decoded.get("email")
        except Exception as e:  # noqa: BLE001
            return jsonify({"error": f"Token inválido: {e}"}), 401
        if (g.email or "").lower() not in ADMIN_EMAILS:
            return jsonify({"error": "Acesso restrito"}), 403
        return f(*args, **kwargs)

    return wrapper


def require_staff(f):
    """Permite admin OU personal trainer (para gerir a biblioteca de treino)."""

    @wraps(f)
    def wrapper(*args, **kwargs):
        header = request.headers.get("Authorization", "")
        if not header.startswith("Bearer "):
            return jsonify({"error": "Token ausente"}), 401
        if not firebase_admin._apps:
            init_firebase()
        try:
            decoded = fb_auth.verify_id_token(header.split(" ", 1)[1])
            g.uid = decoded["uid"]
            g.email = decoded.get("email")
        except Exception as e:  # noqa: BLE001
            return jsonify({"error": f"Token inválido: {e}"}), 401
        em = (g.email or "").lower()
        if em not in ADMIN_EMAILS and em not in PERSONAL_EMAILS:
            return jsonify({"error": "Acesso restrito à equipe"}), 403
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
    return jsonify({"status": "ok", "model": GEMINI_MODEL})


@app.route("/models")
def list_models():
    """Diagnóstico: lista os modelos disponíveis para a chave configurada."""
    if not GEMINI_API_KEY:
        return jsonify({"error": "GEMINI_API_KEY não configurada"}), 500
    try:
        genai.configure(api_key=GEMINI_API_KEY)
        disponiveis = [
            m.name
            for m in genai.list_models()
            if "generateContent" in getattr(m, "supported_generation_methods", [])
        ]
        return jsonify({"modelo_atual": GEMINI_MODEL, "disponiveis": disponiveis})
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------------
# API: análise de PDF de bioimpedância
# ---------------------------------------------------------------------------
PDF_EXTRACT_PROMPT = """Você é um assistente que extrai dados de exames de \
bioimpedância. Analise o PDF anexado e devolva APENAS um objeto JSON válido \
(sem markdown, sem explicação) com as chaves abaixo. Use null quando o dado \
não estiver presente no exame.

{
  "data_exame": string|null,
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
  "cintura_cm": number|null,
  "quadril_cm": number|null,
  "observacoes": string|null
}

Regras:
- "data_exame" no formato AAAA-MM-DD (data em que o exame foi feito).
- "percentual_gordura" é o % de gordura ATUAL medido; se o laudo só trouxer a \
META de %G (e não o valor atual), deixe null.
- "cintura_cm" e "quadril_cm" vêm da perimetria/circunferências, quando houver.
- Extraia todos os campos presentes no documento, mesmo em laudos de dobras \
cutâneas (protocolo Pollock) ou bioimpedância.
Responda somente com o JSON."""


@app.route("/api/analyze-pdf", methods=["POST"])
@require_auth
def analyze_pdf():
    """Recebe um PDF (multipart 'file' ou JSON base64) e extrai os dados."""
    pdf_bytes = None

    if "file" in request.files:
        f = request.files["file"]
        mt = (f.mimetype or "").lower()
        if mt and mt != "application/pdf" and not (f.filename or "").lower().endswith(".pdf"):
            return jsonify({"error": "Envie a bioimpedância em PDF."}), 400
        pdf_bytes = f.read()
    else:
        data = request.get_json(silent=True) or {}
        if data.get("pdf_base64"):
            pdf_bytes = base64.standard_b64decode(data["pdf_base64"])

    if not pdf_bytes:
        return jsonify({"error": "Nenhum PDF enviado"}), 400

    try:
        resp = gemini().generate_content(
            [
                {"mime_type": "application/pdf", "data": pdf_bytes},
                PDF_EXTRACT_PROMPT,
            ]
        )
        raw = (resp.text or "").strip()
        parsed = _safe_json(raw)
        return jsonify({"data": parsed, "raw": raw})
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------------
# API: análise de exame de sangue
# ---------------------------------------------------------------------------
BLOOD_EXTRACT_PROMPT = """Você extrai resultados de exames de sangue. Analise o \
documento anexado (PDF ou imagem) e devolva APENAS um objeto JSON válido \
(sem markdown) no formato:

{
  "data_exame": string|null,
  "marcadores": [
    {
      "nome": string,            // ex.: "Testosterona Total", "TSH", "Glicose", "HDL", "PCR Ultra Sensível", "Vitamina D"
      "valor": string,           // ex.: "112 mg/dL"
      "referencia": string|null, // faixa de referência do laudo, se houver
      "status": "normal"|"atencao"|"alterado"|null,  // sua avaliação vs. referência
      "categoria": "hormonal"|"tireoide"|"metabolico"|"lipidico"|"inflamacao"|"vitaminas"|"hemograma"|"outros"
    }
  ]
}

Priorize e classifique corretamente na categoria:
- hormonal: Testosterona Total, Testosterona Livre, SHBG, Estradiol
- tireoide: TSH, T4 Livre, T3
- metabolico: Glicemia/Glicose, Hemoglobina Glicada (HbA1c)
- lipidico: HDL, LDL, Triglicerídeos, Colesterol Total
- inflamacao: PCR Ultra Sensível
- vitaminas: Vitamina D, B12, Ferritina
Inclua também outros marcadores relevantes que encontrar. Responda somente com o JSON."""


_ALLOWED_MEDIA = {
    "application/pdf": "application/pdf",
    "image/png": "image/png",
    "image/jpeg": "image/jpeg",
    "image/jpg": "image/jpeg",
    "image/webp": "image/webp",
}


@app.route("/api/analyze-blood", methods=["POST"])
@require_auth
def analyze_blood():
    """Recebe um PDF ou imagem de exame de sangue e extrai os marcadores."""
    if "file" not in request.files:
        return jsonify({"error": "Nenhum arquivo enviado"}), 400
    f = request.files["file"]
    media = _ALLOWED_MEDIA.get((f.mimetype or "").lower())
    if not media:
        return jsonify({"error": "Formato não suportado. Envie PDF ou imagem (PNG/JPG)."}), 400
    file_bytes = f.read()
    try:
        resp = gemini().generate_content(
            [
                {"mime_type": media, "data": file_bytes},
                BLOOD_EXTRACT_PROMPT,
            ]
        )
        raw = (resp.text or "").strip()
        parsed = _safe_json(raw)
        return jsonify({"data": parsed, "raw": raw})
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------------
# API: análise de saúde (bioimpedância + sangue)
# ---------------------------------------------------------------------------
HEALTH_ANALYSIS_SYSTEM = """Você é um profissional de saúde que interpreta \
dados de bioimpedância e exames de sangue de forma clara e acolhedora, para \
leigos. NÃO faça diagnóstico nem prescreva medicação. Aponte o que merece \
atenção e o que está bem, com linguagem simples.

Devolva APENAS um JSON válido (sem markdown) no formato:
{
  "resumo": string,
  "destaques": [
    {
      "titulo": string,          // ex.: "Colesterol LDL elevado"
      "valor": string,           // ex.: "165 mg/dL (ref. < 130)"
      "status": "bom"|"atencao"|"alerta",
      "origem": "bioimpedancia"|"sangue",
      "comentario": string       // 1-2 frases explicando e o que fazer na alimentação
    }
  ],
  "recomendacoes_dieta": [string],
  "suplementos_sugeridos": [
    {"nome": string, "motivo": string}  // ex.: {"nome":"Vitamina D3","motivo":"nível abaixo da faixa ideal"}
  ],
  "aviso": "Interpretação gerada por IA. Leve seus exames a um médico ou nutricionista antes de suplementar."
}

Ordene os destaques do mais importante para o menos. Priorize itens com status \
'alerta' e 'atencao'. Foque no que dá para melhorar pela alimentação. Em \
"suplementos_sugeridos", indique apenas o que os exames justificarem (ex.: \
vitamina D baixa, ferritina baixa, ômega-3 para HDL baixo); deixe a lista \
vazia se não houver indicação clara."""


@app.route("/api/health-analysis", methods=["POST"])
@require_auth
def health_analysis():
    """Gera a análise dos exames a partir do perfil + marcadores salvos."""
    perfil = db().collection("usuarios").document(g.uid).get().to_dict() or {}
    bio = {k: perfil.get(k) for k in (
        "sexo", "idade", "altura_cm", "peso_kg", "imc", "percentual_gordura",
        "massa_magra_kg", "gordura_visceral", "taxa_metabolica_basal_kcal") if perfil.get(k) is not None}
    sangue = perfil.get("exames_sangue") or {}

    if not bio and not sangue:
        return jsonify({"error": "Nenhum dado de exame para analisar. Preencha o perfil ou anexe um exame."}), 400

    user_msg = (
        "Dados de bioimpedância / composição corporal:\n"
        + json.dumps(bio, ensure_ascii=False, indent=2)
        + "\n\nExames de sangue:\n"
        + json.dumps(sangue, ensure_ascii=False, indent=2)
        + "\n\nGere a análise no formato JSON solicitado."
    )
    try:
        resp = gemini(system_instruction=HEALTH_ANALYSIS_SYSTEM).generate_content(user_msg)
        raw = (resp.text or "").strip()
        analise = _safe_json(raw)
        analise["gerado_em"] = datetime.datetime.utcnow().isoformat()
        db().collection("usuarios").document(g.uid).set({"analise": analise}, merge=True)
        return jsonify(analise)
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
      "itens": [{"alimento": string, "porcao": string, "kcal": number, "grupo": "proteina"|"carboidrato"|"fruta"|"outro"}],
      "kcal_total": number
    }
  ],
  "substituicoes": {
    "proteinas": [{"alimento": string, "porcao": string, "kcal": number}],
    "carboidratos": [{"alimento": string, "porcao": string, "kcal": number}],
    "frutas": [{"alimento": string, "porcao": string, "kcal": number}]
  },
  "suplementos": [
    {"nome": string, "motivo": string}  // só se os exames justificarem; senão lista vazia
  ],
  "observacoes": string,
  "aviso": "Este plano é uma sugestão gerada por IA e não substitui um nutricionista."
}

Classifique cada item das refeições no campo "grupo": "proteina" para carnes, \
ovos, laticínios proteicos e leguminosas; "carboidrato" para arroz, pães, \
tubérculos, massas e cereais; "fruta" para frutas; "outro" para vegetais, \
gorduras, bebidas e temperos.

Em "substituicoes", forneça de 5 a 7 opções equivalentes em cada grupo \
(proteinas, carboidratos, frutas), com a porção ajustada para calorias \
parecidas, priorizando alimentos acessíveis no Brasil, para o usuário poder \
trocar itens da dieta mantendo o equilíbrio."""


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

    # contexto clínico salvo (exame de sangue e análise), se houver
    salvo = db().collection("usuarios").document(g.uid).get().to_dict() or {}
    exames_sangue = salvo.get("exames_sangue") or {}
    analise = salvo.get("analise") or {}
    contexto = ""
    if exames_sangue.get("marcadores"):
        contexto += "\n\nExame de sangue (marcadores):\n" + json.dumps(exames_sangue, ensure_ascii=False, indent=2)
    if analise.get("destaques"):
        contexto += "\n\nAnálise de saúde já feita (destaques):\n" + json.dumps(analise.get("destaques"), ensure_ascii=False, indent=2)

    user_msg = f"""Dados do usuário:
{json.dumps(perfil, ensure_ascii=False, indent=2)}

Objetivo: {objetivo}
Prazo do objetivo: {prazo}
Rotina de treino (com horários): {rotina_treino}
Rotina alimentar atual e horários: {rotina_alimentar}
Restrições / preferências alimentares: {restricoes or "nenhuma informada"}{contexto}

Leve em conta a composição corporal e, se houver, os exames de sangue e a \
análise acima: ajuste a dieta a alterações relevantes e sugira suplementos \
apenas quando os exames justificarem. Monte a dieta no formato JSON solicitado."""

    try:
        resp = gemini(system_instruction=DIET_SYSTEM).generate_content(user_msg)
        raw = (resp.text or "").strip()
        dieta = _safe_json(raw)

        registro = {
            "nome": perfil.get("nome") or (g.email or "").split("@")[0],
            "perfil": perfil,
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


@app.route("/api/diets/<did>", methods=["PUT"])
@require_auth
def update_diet(did):
    """Atualiza o conteúdo de uma dieta (edição manual: horários, itens...)."""
    data = request.get_json(silent=True) or {}
    dieta = data.get("dieta")
    if not isinstance(dieta, dict):
        return jsonify({"error": "dieta inválida"}), 400
    ref = db().collection("usuarios").document(g.uid).collection("dietas").document(did)
    if not ref.get().exists:
        return jsonify({"error": "dieta não encontrada"}), 404
    ref.set({"dieta": dieta, "editado_em": datetime.datetime.utcnow().isoformat()}, merge=True)
    return jsonify({"ok": True})


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
# API: medições ao longo do tempo (peso, gordura, massa, visceral, cintura)
# ---------------------------------------------------------------------------
MEDICAO_CAMPOS = [
    "peso_kg", "percentual_gordura", "massa_magra_kg",
    "gordura_visceral", "cintura_cm", "quadril_cm",
]


@app.route("/api/measurements", methods=["GET"])
@require_auth
def list_measurements():
    docs = (
        db().collection("usuarios").document(g.uid).collection("medicoes")
        .order_by("data").limit(200).stream()
    )
    out = []
    for d in docs:
        item = d.to_dict()
        item["id"] = d.id
        out.append(item)
    return jsonify(out)


@app.route("/api/measurements", methods=["POST"])
@require_auth
def add_measurement():
    data = request.get_json(silent=True) or {}
    # data = data da MEDIÇÃO (exame); criado_em = data de INSERÇÃO no app
    reg = {"data": data.get("data") or datetime.date.today().isoformat()}
    if data.get("nome"):
        reg["nome"] = data.get("nome")
    if data.get("fonte"):
        reg["fonte"] = data.get("fonte")  # ex.: "bioimpedancia", "manual"
    for c in MEDICAO_CAMPOS:
        v = data.get(c)
        if v is not None and v != "":
            try:
                reg[c] = float(v)
            except (TypeError, ValueError):
                pass
    reg["criado_em"] = datetime.datetime.utcnow().isoformat()
    base = db().collection("usuarios").document(g.uid)
    ref = base.collection("medicoes").add(reg)
    reg["id"] = ref[1].id

    # o perfil reflete a medição MAIS RECENTE por data do exame (não a última inserida)
    recentes = list(base.collection("medicoes")
                    .order_by("data", direction=firestore.Query.DESCENDING).limit(1).stream())
    if recentes:
        ult = recentes[0].to_dict() or {}
        perfil_update = {c: ult[c] for c in MEDICAO_CAMPOS if ult.get(c) is not None}
        if ult.get("data"):
            perfil_update["medicao_data"] = ult["data"]
        if perfil_update:
            base.set(perfil_update, merge=True)
    return jsonify(reg)


# ---------------------------------------------------------------------------
# API: Telegram (detectar chat, testar envio)
# ---------------------------------------------------------------------------
@app.route("/api/telegram/detect", methods=["GET"])
@require_auth
def telegram_detect():
    """Lista chats que mandaram mensagem recente ao bot (para pegar o chat_id)."""
    try:
        return jsonify({"chats": tg_updates()})
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": str(e)}), 500


@app.route("/api/telegram/test", methods=["POST"])
@require_auth
def telegram_test():
    """Envia uma mensagem de teste para o chat_id informado/salvo."""
    data = request.get_json(silent=True) or {}
    chat_id = data.get("chat_id") or (
        db().collection("usuarios").document(g.uid).get().to_dict() or {}
    ).get("telegram_chat_id")
    if not chat_id:
        return jsonify({"error": "chat_id não informado"}), 400
    try:
        tg_send(chat_id, "✅ <b>Minha Dieta</b>\nLembretes conectados! Você vai receber "
                         "suas refeições nos horários da dieta.")
        return jsonify({"ok": True})
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": str(e)}), 500


@app.route("/api/telegram/link-code", methods=["POST"])
@require_auth
def telegram_link_code():
    """Gera um código e o deep-link para o usuário conectar o Telegram."""
    code = secrets.token_hex(3)
    db().collection("tg_links").document(code).set(
        {"uid": g.uid, "criado_em": datetime.datetime.utcnow().isoformat()})
    link = f"https://t.me/{TELEGRAM_BOT_USERNAME}?start={code}"
    return jsonify({"code": code, "link": link})


# ---------------------------------------------------------------------------
# CRON: enviar lembretes das refeições que estão na hora
# ---------------------------------------------------------------------------
@app.route("/cron/send-reminders", methods=["GET", "POST"])
def cron_send_reminders():
    """Chamado por um cron externo. Protegido por CRON_SECRET.

    Para cada usuário com telegram_chat_id, pega a dieta mais recente e envia
    as refeições cujo horário caiu na janela atual, evitando duplicar no dia.
    """
    secret = request.args.get("secret") or request.headers.get("X-Cron-Secret")
    if not CRON_SECRET or secret != CRON_SECRET:
        return jsonify({"error": "não autorizado"}), 401

    agora = now_local()
    hoje = agora.strftime("%Y-%m-%d")
    enviados_total = 0
    detalhes = []

    usuarios_ok = 0
    for user_doc in db().collection("usuarios").stream():
        try:
            perfil = user_doc.to_dict() or {}
            chat_id = perfil.get("telegram_chat_id")
            if not chat_id:
                continue

            # dieta mais recente
            dietas = list(
                db().collection("usuarios").document(user_doc.id)
                .collection("dietas")
                .order_by("criado_em", direction=firestore.Query.DESCENDING)
                .limit(1).stream()
            )
            if not dietas:
                continue
            diet_id = dietas[0].id
            dieta = (dietas[0].to_dict() or {}).get("dieta") or {}
            refeicoes = dieta.get("refeicoes") or []
            usuarios_ok += 1

            # marcador de enviados do dia
            marca_ref = (db().collection("usuarios").document(user_doc.id)
                         .collection("lembretes").document(hoje))
            ja_enviados = set((marca_ref.get().to_dict() or {}).get("enviados", []))

            for idx, ref in enumerate(refeicoes):
                if not isinstance(ref, dict):
                    continue
                hm = parse_horario(ref.get("horario"))
                if not hm:
                    continue
                alvo = agora.replace(hour=hm[0], minute=hm[1], second=0, microsecond=0)
                delta_min = (agora - alvo).total_seconds() / 60.0
                chave = f"{diet_id}:{idx}"  # por dieta+refeição (nova dieta não colide)
                if 0 <= delta_min <= REMINDER_WINDOW_MIN and chave not in ja_enviados:
                    try:
                        botoes = {"inline_keyboard": [[
                            {"text": "✅ Cumpri", "callback_data": f"ok|{hoje}|{idx}|{diet_id}"},
                            {"text": "🔄 Outra coisa", "callback_data": f"other|{hoje}|{idx}|{diet_id}"},
                            {"text": "⏭️ Pulei", "callback_data": f"skip|{hoje}|{idx}|{diet_id}"},
                        ]]}
                        tg_send(chat_id, format_meal_message(ref), reply_markup=botoes)
                        ja_enviados.add(chave)
                        enviados_total += 1
                        detalhes.append({"uid": user_doc.id, "refeicao": ref.get("nome")})
                    except Exception as e:  # noqa: BLE001
                        detalhes.append({"uid": user_doc.id, "erro_envio": str(e)})

            marca_ref.set({"enviados": list(ja_enviados)}, merge=True)
        except Exception as e:  # noqa: BLE001
            # um usuário com problema não pode interromper os demais
            detalhes.append({"uid": user_doc.id, "erro_usuario": str(e)})
            continue

    return jsonify({"hora": agora.isoformat(), "usuarios_processados": usuarios_ok,
                    "enviados": enviados_total, "detalhes": detalhes})


# ---------------------------------------------------------------------------
# Telegram Webhook: recebe respostas e registra consumo/aderência
# ---------------------------------------------------------------------------
def find_uid_by_chat(chat_id):
    q = (db().collection("usuarios")
         .where("telegram_chat_id", "==", str(chat_id)).limit(1).stream())
    for d in q:
        return d.id
    return None


def latest_diet(uid):
    docs = list(db().collection("usuarios").document(uid).collection("dietas")
                .order_by("criado_em", direction=firestore.Query.DESCENDING).limit(1).stream())
    return (docs[0].to_dict() or {}).get("dieta") if docs else None


def update_consumo(uid, dia, idx, entry):
    ref = db().collection("usuarios").document(uid).collection("consumo").document(dia)
    data = ref.get().to_dict() or {"data": dia, "refeicoes": {}}
    if data.get("alvo_kcal") is None:
        data["alvo_kcal"] = (latest_diet(uid) or {}).get("calorias_alvo_dia")
    refs = data.get("refeicoes") or {}
    refs[str(idx)] = entry
    data["refeicoes"] = refs
    data["total_kcal"] = sum((e.get("kcal") or 0) for e in refs.values())
    data["atualizado_em"] = datetime.datetime.utcnow().isoformat()
    ref.set(data)
    return data["total_kcal"]


def add_pending(uid, dia, idx, diet_id):
    """Guarda um pendente por refeição (lista), sem sobrescrever outros."""
    ref = db().collection("usuarios").document(uid)
    lst = (ref.get().to_dict() or {}).get("tg_pendentes") or []
    lst = [p for p in lst if not (p.get("data") == dia and p.get("idx") == idx and p.get("diet_id") == diet_id)]
    lst.append({"data": dia, "idx": idx, "diet_id": diet_id})
    ref.set({"tg_pendentes": lst[-6:]}, merge=True)


def peek_pending(uid):
    lst = (db().collection("usuarios").document(uid).get().to_dict() or {}).get("tg_pendentes") or []
    return lst[-1] if lst else None


def remove_pending(uid, dia, idx, diet_id):
    ref = db().collection("usuarios").document(uid)
    lst = (ref.get().to_dict() or {}).get("tg_pendentes") or []
    lst = [p for p in lst if not (p.get("data") == dia and p.get("idx") == idx and p.get("diet_id") == diet_id)]
    ref.set({"tg_pendentes": lst}, merge=True)


def diet_by_id(uid, diet_id):
    """Carrega a dieta pelo id; se faltar/não existir, usa a mais recente."""
    if diet_id:
        doc = db().collection("usuarios").document(uid).collection("dietas").document(diet_id).get()
        if doc.exists:
            return (doc.to_dict() or {}).get("dieta") or {}
    return latest_diet(uid) or {}


def _meal_from(uid, diet_id, idx):
    refs = (diet_by_id(uid, diet_id) or {}).get("refeicoes") or []
    return refs[idx] if 0 <= idx < len(refs) else {}


def estimate_calories(text):
    """Estima calorias. Retorna {ok, kcal_total, itens} ou {ok: False, erro}."""
    prompt = ("Estime as calorias do que a pessoa comeu. Responda APENAS JSON "
              '{"itens":[{"alimento":string,"kcal":number}],"kcal_total":number}. '
              f"Comida: {text}")
    try:
        resp = gemini().generate_content(prompt)
        parsed = _safe_json((resp.text or "").strip())
        if parsed.get("_parse_error"):
            return {"ok": False, "erro": "resposta não estruturada"}
        kcal = parsed.get("kcal_total")
        if not kcal:
            kcal = sum((i.get("kcal") or 0) for i in parsed.get("itens", []))
        if not kcal or kcal <= 0:
            return {"ok": False, "erro": "sem estimativa"}
        return {"ok": True, "kcal_total": int(kcal), "itens": parsed.get("itens", [])}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "erro": str(e)}


def handle_callback(cq):
    cid = cq.get("id")
    chat = cq.get("message", {}).get("chat", {}).get("id")
    mid = cq.get("message", {}).get("message_id")
    parts = (cq.get("data") or "").split("|")
    if len(parts) < 3:
        tg_answer_callback(cid); return
    action, dia, idx = parts[0], parts[1], int(parts[2])
    diet_id = parts[3] if len(parts) > 3 else None
    uid = find_uid_by_chat(chat)
    if not uid:
        tg_answer_callback(cid, "Conta não encontrada."); return
    ref = _meal_from(uid, diet_id, idx)  # a refeição da dieta CERTA (não a mais recente)
    nome = ref.get("nome", "Refeição")

    if action == "ok":
        kcal = ref.get("kcal_total") or 0
        total = update_consumo(uid, dia, idx, {"nome": nome, "status": "cumpriu", "kcal": kcal})
        tg_answer_callback(cid, "Registrado ✅")
        tg_remove_keyboard(chat, mid)
        tg_send(chat, f"✅ <b>{_esc(nome)}</b> cumprida (+{kcal} kcal). Total de hoje: <b>{total} kcal</b>.")
    elif action == "skip":
        total = update_consumo(uid, dia, idx, {"nome": nome, "status": "pulou", "kcal": 0})
        tg_answer_callback(cid, "Anotado")
        tg_remove_keyboard(chat, mid)
        tg_send(chat, f"⏭️ <b>{_esc(nome)}</b> marcada como pulada. Total de hoje: <b>{total} kcal</b>.")
    elif action == "other":
        add_pending(uid, dia, idx, diet_id)
        tg_answer_callback(cid, "Me conta o que comeu")
        tg_remove_keyboard(chat, mid)
        tg_send(chat, f"O que você comeu no lugar de <b>{_esc(nome)}</b>? Escreva aqui que eu calculo as calorias.")
    else:
        tg_answer_callback(cid)


def handle_message(msg):
    chat = msg.get("chat", {}).get("id")
    text = (msg.get("text") or "").strip()

    # /start <code> -> vincula o chat a um usuário do app
    if text.startswith("/start"):
        parts = text.split(maxsplit=1)
        if len(parts) > 1:
            code = parts[1].strip()
            link = db().collection("tg_links").document(code).get()
            if link.exists:
                luid = (link.to_dict() or {}).get("uid")
                db().collection("usuarios").document(luid).set({"telegram_chat_id": str(chat)}, merge=True)
                db().collection("tg_links").document(code).delete()
                tg_send(chat, "✅ <b>Telegram conectado!</b> Você vai receber os lembretes das refeições aqui e pode registrar o que comeu.")
                return
        tg_send(chat, "Oi! Para conectar, abra o app, vá em Criar perfil → Telegram e toque em “Conectar meu Telegram”.")
        return

    uid = find_uid_by_chat(chat)
    if not uid:
        return
    pend = peek_pending(uid)
    if not pend:
        tg_send(chat, "Recebi 👍 Quando chegar o horário de uma refeição, use os botões para registrar (Cumpri / Outra coisa / Pulei).")
        return

    idx, dia, diet_id = pend.get("idx"), pend.get("data"), pend.get("diet_id")
    nome = _meal_from(uid, diet_id, idx).get("nome", "Refeição")

    # atalho: usuário mandou só as calorias (ex.: "520" ou "520 kcal")
    mnum = re.match(r"^\s*(\d{1,5})\s*(kcal|cal)?\s*$", text, re.IGNORECASE)
    if mnum:
        kcal = int(mnum.group(1))
    else:
        est = estimate_calories(text)
        if not est.get("ok"):
            # NÃO registra 0 kcal em caso de falha — mantém pendente e pede de novo
            tg_send(chat, "Não consegui calcular agora 😕. Tente escrever de outro jeito "
                          "(ex.: <i>2 pães com queijo e 1 café</i>) ou me diga só as calorias, tipo <b>520 kcal</b>.")
            return
        kcal = int(est.get("kcal_total") or 0)

    total = update_consumo(uid, dia, idx, {"nome": nome, "status": "substituiu", "itens": text, "kcal": kcal})
    remove_pending(uid, dia, idx, diet_id)
    tg_send(chat, f"Anotei no lugar de <b>{_esc(nome)}</b>: “{_esc(text)}” ≈ <b>{kcal} kcal</b>.\nTotal de hoje: <b>{total} kcal</b>.")


@app.route("/telegram/webhook", methods=["POST"])
def telegram_webhook():
    if request.headers.get("X-Telegram-Bot-Api-Secret-Token") != TELEGRAM_WEBHOOK_SECRET:
        return jsonify({"ok": False}), 403
    upd = request.get_json(silent=True) or {}
    try:
        if "callback_query" in upd:
            handle_callback(upd["callback_query"])
        elif "message" in upd and upd["message"].get("text"):
            handle_message(upd["message"])
    except Exception as e:  # noqa: BLE001
        print("[webhook] erro:", e)
    return jsonify({"ok": True})


@app.route("/telegram/setup", methods=["GET", "POST"])
def telegram_setup():
    if (request.args.get("secret") or "") != CRON_SECRET:
        return jsonify({"error": "não autorizado"}), 401
    base = (request.args.get("base") or PUBLIC_URL or "").rstrip("/")
    if not base:
        return jsonify({"error": "URL pública não definida. Passe ?base=https://seu-app.onrender.com"}), 400
    url = base + "/telegram/webhook"
    # apaga primeiro para garantir que o secret_token atual seja aplicado
    try:
        tg_call("deleteWebhook", {"drop_pending_updates": "false"})
    except Exception:  # noqa: BLE001
        pass
    res = tg_call("setWebhook", {
        "url": url,
        "secret_token": TELEGRAM_WEBHOOK_SECRET,
        "allowed_updates": json.dumps(["message", "callback_query"]),
    })
    return jsonify({"webhook": url, "resultado": res})


@app.route("/telegram/info", methods=["GET"])
def telegram_info():
    """Diagnóstico do webhook (getWebhookInfo)."""
    if (request.args.get("secret") or "") != CRON_SECRET:
        return jsonify({"error": "não autorizado"}), 401
    try:
        return jsonify(tg_call("getWebhookInfo", {}))
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": str(e)}), 500


@app.route("/api/consumo/today", methods=["GET"])
@require_auth
def consumo_today():
    dia = now_local().strftime("%Y-%m-%d")
    d = (db().collection("usuarios").document(g.uid)
         .collection("consumo").document(dia).get().to_dict()) or {}
    return jsonify(d)


@app.route("/api/consumo/registrar", methods=["POST"])
@require_auth
def consumo_registrar():
    """Registra uma refeição direto pelo app (Cumpri/Troquei/Pulei)."""
    d = request.get_json(silent=True) or {}
    dia = d.get("dia") or now_local().strftime("%Y-%m-%d")
    try:
        idx = int(d.get("idx"))
    except (TypeError, ValueError):
        return jsonify({"error": "idx inválido"}), 400
    status = d.get("status")
    if status not in ("cumpriu", "substituiu", "pulou"):
        return jsonify({"error": "status inválido"}), 400

    refs = (latest_diet(g.uid) or {}).get("refeicoes") or []
    ref = refs[idx] if 0 <= idx < len(refs) else {}
    nome = ref.get("nome", "Refeição")

    if status == "cumpriu":
        entry = {"nome": nome, "status": "cumpriu", "kcal": int(ref.get("kcal_total") or 0)}
    elif status == "pulou":
        entry = {"nome": nome, "status": "pulou", "kcal": 0}
    else:  # substituiu
        try:
            kcal = int(d.get("kcal"))
        except (TypeError, ValueError):
            return jsonify({"error": "Informe as calorias da substituição."}), 400
        entry = {"nome": nome, "status": "substituiu",
                 "itens": (d.get("itens") or "").strip(), "kcal": kcal}

    total = update_consumo(g.uid, dia, idx, entry)
    return jsonify({"ok": True, "total_kcal": total})


@app.route("/api/consumo/remover", methods=["POST"])
@require_auth
def consumo_remover():
    """Desfaz o registro de uma refeição (volta para pendente)."""
    d = request.get_json(silent=True) or {}
    dia = d.get("dia") or now_local().strftime("%Y-%m-%d")
    idx = str(d.get("idx"))
    ref = (db().collection("usuarios").document(g.uid)
           .collection("consumo").document(dia))
    data = ref.get().to_dict() or {}
    refs = data.get("refeicoes") or {}
    if idx in refs:
        del refs[idx]
        data["refeicoes"] = refs
        data["total_kcal"] = sum((e.get("kcal") or 0) for e in refs.values())
        ref.set(data)
    return jsonify({"ok": True, "total_kcal": data.get("total_kcal", 0)})


@app.route("/api/estimate-kcal", methods=["POST"])
@require_auth
def estimate_kcal_route():
    """Estima calorias de um texto (para a substituição feita no app)."""
    texto = ((request.get_json(silent=True) or {}).get("texto") or "").strip()
    if not texto:
        return jsonify({"error": "texto vazio"}), 400
    return jsonify(estimate_calories(texto))


# ---------------------------------------------------------------------------
# Identidade e Admin (somente leitura)
# ---------------------------------------------------------------------------
@app.route("/api/me", methods=["GET"])
@require_auth
def me():
    em = (g.email or "").lower()
    return jsonify({"email": g.email,
                    "is_admin": em in ADMIN_EMAILS,
                    "is_personal": em in PERSONAL_EMAILS,
                    "is_staff": em in ADMIN_EMAILS or em in PERSONAL_EMAILS})


# ---------------------------------------------------------------------------
# Biblioteca de exercícios (RF-09) — gerida por admin/personal
# ---------------------------------------------------------------------------
EXERCICIO_CAMPOS = [
    "nome", "aliases", "grupo_muscular", "padrao_movimento", "equipamento",
    "dificuldade", "instrucoes", "midia_url", "substituicoes",
    "contraindicacoes", "status",
]
_STATUS_EXERCICIO = {"rascunho", "aprovado", "inativo"}


def _clean_exercicio(data):
    """Normaliza o payload do exercício (allowlist de campos)."""
    out = {}
    for c in EXERCICIO_CAMPOS:
        if c in data:
            out[c] = data[c]
    # listas
    for lc in ("aliases", "substituicoes"):
        v = out.get(lc)
        if isinstance(v, str):
            out[lc] = [s.strip() for s in v.split(",") if s.strip()]
        elif not isinstance(v, list):
            out[lc] = []
    if out.get("status") not in _STATUS_EXERCICIO:
        out["status"] = "rascunho"
    return out


@app.route("/api/exercicios", methods=["GET"])
@require_staff
def exercicios_list():
    docs = db().collection("exercicios").order_by("nome").limit(500).stream()
    out = []
    for d in docs:
        item = d.to_dict() or {}
        item["id"] = d.id
        out.append(item)
    return jsonify(out)


@app.route("/api/exercicios", methods=["POST"])
@require_staff
def exercicios_create():
    data = _clean_exercicio(request.get_json(silent=True) or {})
    if not data.get("nome"):
        return jsonify({"error": "Nome é obrigatório."}), 400
    agora = datetime.datetime.utcnow().isoformat()
    data.update({"versao": 1, "criado_por": g.email, "criado_em": agora,
                 "atualizado_por": g.email, "atualizado_em": agora})
    ref = db().collection("exercicios").add(data)
    data["id"] = ref[1].id
    return jsonify(data)


@app.route("/api/exercicios/<eid>", methods=["PUT"])
@require_staff
def exercicios_update(eid):
    ref = db().collection("exercicios").document(eid)
    atual = ref.get()
    if not atual.exists:
        return jsonify({"error": "Exercício não encontrado."}), 404
    prev = atual.to_dict() or {}
    # snapshot da versão anterior (histórico)
    ref.collection("versoes").document(str(prev.get("versao", 1))).set(prev)
    data = _clean_exercicio(request.get_json(silent=True) or {})
    data["versao"] = int(prev.get("versao", 1)) + 1
    data["atualizado_por"] = g.email
    data["atualizado_em"] = datetime.datetime.utcnow().isoformat()
    ref.set(data, merge=True)
    out = ref.get().to_dict() or {}
    out["id"] = eid
    return jsonify(out)


@app.route("/api/exercicios/<eid>/status", methods=["POST"])
@require_staff
def exercicios_status(eid):
    novo = (request.get_json(silent=True) or {}).get("status")
    if novo not in _STATUS_EXERCICIO:
        return jsonify({"error": "status inválido"}), 400
    ref = db().collection("exercicios").document(eid)
    if not ref.get().exists:
        return jsonify({"error": "Exercício não encontrado."}), 404
    ref.set({"status": novo, "atualizado_por": g.email,
             "atualizado_em": datetime.datetime.utcnow().isoformat()}, merge=True)
    return jsonify({"ok": True, "status": novo})


# ---------------------------------------------------------------------------
# Planos de treino (rascunho) — geração assistida por IA + edição
# A IA recebe SOMENTE dados não sensíveis (objetivo, nível, dias, equipamento)
# e a biblioteca de exercícios APROVADOS. Nenhum dado de saúde é enviado.
# ---------------------------------------------------------------------------
def _exercicios_aprovados():
    out = {}
    for d in db().collection("exercicios").where("status", "==", "aprovado").limit(500).stream():
        e = d.to_dict() or {}
        out[d.id] = {"id": d.id, "nome": e.get("nome"),
                     "grupo_muscular": e.get("grupo_muscular"),
                     "equipamento": e.get("equipamento"),
                     "padrao_movimento": e.get("padrao_movimento")}
    return out


TREINO_SYSTEM = """Você é um assistente que monta um RASCUNHO de plano de treino \
para revisão de um personal trainer. Use APENAS os exercícios da lista fornecida \
(pelo campo id). NUNCA invente exercícios fora da lista. O resultado é sugestivo \
e será revisado por um profissional antes de publicar.

Devolva APENAS JSON válido:
{
  "nome": string,
  "objetivo": string,
  "nivel": string,
  "dias_por_semana": number,
  "sessoes": [
    {
      "dia_semana": "seg"|"ter"|"qua"|"qui"|"sex"|"sab"|"dom",
      "foco": string,
      "kcal_estimado": number,
      "exercicios": [
        {"exercicio_id": string, "series": number, "reps": string,
         "carga": string, "intervalo_s": number, "obs": string}
      ]
    }
  ],
  "observacoes": string
}
Regras:
- Crie exatamente 'dias_por_semana' sessões, cada uma num dia_semana diferente, \
bem distribuídas na semana (evite dias seguidos quando possível).
- Considere as modalidades já praticadas e o local de treino informados: se a \
pessoa já treina algo intenso, ajuste volume para não sobrecarregar; adapte os \
exercícios ao equipamento do local (casa, academia ou rua).
- Séries, repetições e intervalos coerentes com o nível.
- "kcal_estimado": estimativa de calorias gastas na sessão (número), coerente com \
o volume e a intensidade.
- Use somente exercicio_id da lista fornecida; nunca invente exercícios."""


LIB_SEED_SYSTEM = """Gere uma biblioteca de exercícios de treino comuns e seguros \
para academia e casa. Responda APENAS JSON:
{"exercicios":[{"nome":string,"grupo_muscular":string,"padrao_movimento":string,
"equipamento":string,"dificuldade":"iniciante"|"intermediario"|"avancado",
"instrucoes":string,"substituicoes":[string]}]}
Cubra peito, costas, pernas, glúteos, ombros, bíceps, tríceps e core, com variações \
de equipamento (barra, halteres, máquina, polia, peso corporal). Cerca de 24 itens."""


@app.route("/api/exercicios/seed", methods=["POST"])
@require_staff
def exercicios_seed():
    """Popula a biblioteca com exercícios gerados por IA (marcados como aprovados)."""
    try:
        resp = gemini(system_instruction=LIB_SEED_SYSTEM).generate_content(
            "Gere a biblioteca inicial de exercícios.")
        parsed = _safe_json((resp.text or "").strip())
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": str(e)}), 502
    existentes = {(d.to_dict() or {}).get("nome", "").strip().lower()
                  for d in db().collection("exercicios").limit(500).stream()}
    agora = datetime.datetime.utcnow().isoformat()
    criados = 0
    for ex in (parsed.get("exercicios") or []):
        nome = (ex.get("nome") or "").strip()
        if not nome or nome.lower() in existentes:
            continue
        data = _clean_exercicio(ex)
        data["nome"] = nome
        data["status"] = "aprovado"
        data.update({"versao": 1, "criado_por": "ia-seed", "criado_em": agora,
                     "atualizado_por": g.email, "atualizado_em": agora})
        db().collection("exercicios").add(data)
        existentes.add(nome.lower())
        criados += 1
    return jsonify({"ok": True, "criados": criados})


@app.route("/api/treino/gerar", methods=["POST"])
@require_auth
def treino_gerar():
    """Gera um plano de treino sugestivo para o usuário a partir da biblioteca."""
    d = request.get_json(silent=True) or {}
    aprovados = _exercicios_aprovados()
    if not aprovados:
        return jsonify({"error": "A biblioteca de exercícios ainda está vazia. Peça à equipe para populá-la."}), 400

    catalogo = [{"id": v["id"], "nome": v["nome"], "grupo": v["grupo_muscular"],
                 "equipamento": v["equipamento"], "padrao": v["padrao_movimento"]}
                for v in aprovados.values()]
    modalidades = d.get("modalidades") or []
    if isinstance(modalidades, str):
        modalidades = [modalidades]
    local = d.get("local") or []
    if isinstance(local, str):
        local = [local]
    user_msg = (
        f"Objetivo: {d.get('objetivo', '')}\n"
        f"Nível: {d.get('nivel', 'basico')}\n"
        f"Dias por semana: {d.get('dias_por_semana', 3)}\n"
        f"Modalidades já praticadas (e tempo/frequência): "
        f"{', '.join(modalidades) or 'nenhuma'}. {d.get('tempo_modalidade', '')}\n"
        f"Onde vai treinar: {', '.join(local) or d.get('equipamento', 'academia')}\n\n"
        f"Exercícios aprovados disponíveis (use só estes id):\n"
        + json.dumps(catalogo, ensure_ascii=False)
    )
    try:
        resp = gemini(system_instruction=TREINO_SYSTEM).generate_content(user_msg)
        plano = _safe_json((resp.text or "").strip())
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": str(e)}), 502

    # Guardrail: mantém só exercícios que existem e estão aprovados
    descartados = 0
    for s in plano.get("sessoes", []) or []:
        validos = []
        for ex in s.get("exercicios", []) or []:
            info = aprovados.get(ex.get("exercicio_id"))
            if info:
                ex["nome"] = info["nome"]
                validos.append(ex)
            else:
                descartados += 1
        s["exercicios"] = validos
    plano["descartados"] = descartados
    plano["aviso"] = ("Plano sugerido por IA a partir da biblioteca. É uma sugestão e "
                      "não substitui a orientação de um profissional de educação física.")
    # duração de 30 dias
    hoje_d = now_local().date()
    plano["data_inicio"] = hoje_d.isoformat()
    plano["data_fim"] = (hoje_d + datetime.timedelta(days=30)).isoformat()
    # preferências informadas (para renovar mantendo contexto)
    plano["prefs"] = {"objetivo": d.get("objetivo", ""), "nivel": d.get("nivel", "basico"),
                      "dias_por_semana": d.get("dias_por_semana", 3),
                      "modalidades": modalidades, "tempo_modalidade": d.get("tempo_modalidade", ""),
                      "local": local}
    plano["criado_em"] = datetime.datetime.utcnow().isoformat()
    ref = db().collection("usuarios").document(g.uid).collection("treinos").add(plano)
    plano["id"] = ref[1].id
    return jsonify({"plano": plano})


@app.route("/api/treino/meu", methods=["GET"])
@require_auth
def treino_meu():
    """Plano de treino mais recente do usuário."""
    docs = list(db().collection("usuarios").document(g.uid).collection("treinos")
                .order_by("criado_em", direction=firestore.Query.DESCENDING).limit(1).stream())
    if not docs:
        return jsonify({})
    p = docs[0].to_dict() or {}
    p["id"] = docs[0].id
    return jsonify(p)


@app.route("/api/treino/today", methods=["GET"])
@require_auth
def treino_today():
    dia = now_local().strftime("%Y-%m-%d")
    d = (db().collection("usuarios").document(g.uid)
         .collection("treino_sessoes").document(dia).get().to_dict()) or {}
    return jsonify(d)


@app.route("/api/treino/semana", methods=["GET"])
@require_auth
def treino_semana():
    """Check-ins da semana atual (segunda a domingo) para o calendário."""
    hoje = now_local()
    monday = hoje - datetime.timedelta(days=hoje.weekday())
    base = db().collection("usuarios").document(g.uid).collection("treino_sessoes")
    dias, checkins = [], {}
    for i in range(7):
        dt = (monday + datetime.timedelta(days=i)).strftime("%Y-%m-%d")
        dias.append({"data": dt, "dow": i})
        doc = base.document(dt).get().to_dict()
        if doc:
            checkins[dt] = doc
    return jsonify({"hoje": hoje.strftime("%Y-%m-%d"), "dias": dias, "checkins": checkins})


@app.route("/api/treino/registrar", methods=["POST"])
@require_auth
def treino_registrar():
    """Marca/desmarca um exercício como feito na sessão de um dia (default hoje)."""
    body = request.get_json(silent=True) or {}
    dia = body.get("data") or now_local().strftime("%Y-%m-%d")
    ref = db().collection("usuarios").document(g.uid).collection("treino_sessoes").document(dia)
    data = ref.get().to_dict() or {"data": dia, "feitos": {}}
    data["sessao_idx"] = body.get("sessao_idx")
    data["plano_id"] = body.get("plano_id")
    feitos = data.get("feitos") or {}
    chave = str(body.get("chave"))
    if body.get("feito"):
        feitos[chave] = True
    else:
        feitos.pop(chave, None)
    data["feitos"] = feitos
    data["atualizado_em"] = datetime.datetime.utcnow().isoformat()
    ref.set(data)
    return jsonify({"ok": True, "feitos": len(feitos)})


@app.route("/api/treino/finalizar", methods=["POST"])
@require_auth
def treino_finalizar():
    """Finaliza a sessão de um dia (check-in). Default hoje."""
    body = request.get_json(silent=True) or {}
    dia = body.get("data") or now_local().strftime("%Y-%m-%d")
    ref = db().collection("usuarios").document(g.uid).collection("treino_sessoes").document(dia)
    data = ref.get().to_dict() or {"data": dia, "feitos": {}}
    data["sessao_idx"] = body.get("sessao_idx")
    data["plano_id"] = body.get("plano_id")
    data["total_exercicios"] = body.get("total_exercicios")
    data["feedback"] = body.get("feedback")  # leve | moderado | intenso
    try:
        data["kcal_sessao"] = int(body.get("kcal") or 0)
    except (TypeError, ValueError):
        data["kcal_sessao"] = 0
    data["finalizado"] = True
    data["finalizado_em"] = datetime.datetime.utcnow().isoformat()
    ref.set(data, merge=True)
    return jsonify({"ok": True})


@app.route("/api/treino/atividade", methods=["POST"])
@require_auth
def treino_atividade():
    """Registra uma atividade livre (ex.: corrida 10km) num dia do calendário."""
    body = request.get_json(silent=True) or {}
    dia = body.get("data") or now_local().strftime("%Y-%m-%d")
    ref = db().collection("usuarios").document(g.uid).collection("treino_sessoes").document(dia)
    data = ref.get().to_dict() or {"data": dia}
    data["atividade"] = (body.get("atividade") or "").strip()
    data["atividade_intensidade"] = body.get("intensidade")
    try:
        data["kcal_atividade"] = int(body.get("kcal") or 0)
    except (TypeError, ValueError):
        data["kcal_atividade"] = 0
    data["atividade_em"] = datetime.datetime.utcnow().isoformat()
    ref.set(data, merge=True)
    return jsonify({"ok": True})


@app.route("/api/treino/estimar-atividade", methods=["POST"])
@require_auth
def treino_estimar_atividade():
    """Estima as calorias gastas numa atividade descrita (sem dados clínicos)."""
    body = request.get_json(silent=True) or {}
    texto = (body.get("texto") or "").strip()
    if not texto:
        return jsonify({"error": "descreva a atividade"}), 400
    peso = (db().collection("usuarios").document(g.uid).get().to_dict() or {}).get("peso_kg")
    prompt = ("Estime as calorias gastas nesta atividade física. Responda APENAS JSON "
              '{"kcal": number}. '
              + (f"Peso da pessoa: {peso} kg. " if peso else "")
              + f"Atividade: {texto}")
    try:
        resp = gemini().generate_content(prompt)
        parsed = _safe_json((resp.text or "").strip())
        kcal = int(parsed.get("kcal") or 0)
        if kcal <= 0:
            return jsonify({"ok": False})
        return jsonify({"ok": True, "kcal": kcal})
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "erro": str(e)})


@app.route("/api/treinos", methods=["GET"])
@require_staff
def treinos_list():
    docs = (db().collection("treinos")
            .order_by("criado_em", direction=firestore.Query.DESCENDING).limit(100).stream())
    out = []
    for d in docs:
        it = d.to_dict() or {}
        it["id"] = d.id
        out.append(it)
    return jsonify(out)


@app.route("/api/treinos", methods=["POST"])
@require_staff
def treinos_create():
    plano = request.get_json(silent=True) or {}
    if not plano.get("nome"):
        return jsonify({"error": "Dê um nome ao treino."}), 400
    agora = datetime.datetime.utcnow().isoformat()
    plano.update({"status": "rascunho", "versao": 1,
                  "criado_por": g.email, "criado_em": agora,
                  "atualizado_por": g.email, "atualizado_em": agora})
    plano.pop("_descartados", None)
    ref = db().collection("treinos").add(plano)
    plano["id"] = ref[1].id
    return jsonify(plano)


@app.route("/api/treinos/<tid>", methods=["PUT"])
@require_staff
def treinos_update(tid):
    ref = db().collection("treinos").document(tid)
    prev = ref.get()
    if not prev.exists:
        return jsonify({"error": "Treino não encontrado."}), 404
    p = prev.to_dict() or {}
    ref.collection("versoes").document(str(p.get("versao", 1))).set(p)
    plano = request.get_json(silent=True) or {}
    plano["versao"] = int(p.get("versao", 1)) + 1
    plano["atualizado_por"] = g.email
    plano["atualizado_em"] = datetime.datetime.utcnow().isoformat()
    plano.pop("_descartados", None)
    ref.set(plano, merge=True)
    return jsonify({"ok": True, "versao": plano["versao"]})


@app.route("/api/admin/users", methods=["GET"])
@require_admin
def admin_users():
    out = []
    for doc in db().collection("usuarios").stream():
        p = doc.to_dict() or {}
        out.append({
            "uid": doc.id,
            "email": p.get("email"),
            "nome": p.get("nome"),
            "sexo": p.get("sexo"),
            "idade": p.get("idade"),
            "peso_kg": p.get("peso_kg"),
            "percentual_gordura": p.get("percentual_gordura"),
            "objetivo": p.get("objetivo"),
            "meta": p.get("meta"),
            "telegram": bool(p.get("telegram_chat_id")),
            "tem_analise": bool(p.get("analise")),
            "atualizado_em": p.get("atualizado_em"),
        })
    out.sort(key=lambda x: x.get("atualizado_em") or "", reverse=True)
    return jsonify(out)


@app.route("/api/admin/users/<uid>", methods=["GET"])
@require_admin
def admin_user_detail(uid):
    base = db().collection("usuarios").document(uid)
    perfil = base.get().to_dict() or {}
    dietas = [dict(d.to_dict(), id=d.id) for d in base.collection("dietas")
              .order_by("criado_em", direction=firestore.Query.DESCENDING).limit(20).stream()]
    medicoes = [dict(m.to_dict(), id=m.id) for m in base.collection("medicoes")
                .order_by("data").limit(200).stream()]
    consumo = [dict(c.to_dict(), id=c.id) for c in base.collection("consumo")
               .order_by("data", direction=firestore.Query.DESCENDING).limit(30).stream()]
    treino_sessoes = [dict(t.to_dict(), id=t.id) for t in base.collection("treino_sessoes")
                      .order_by("data", direction=firestore.Query.DESCENDING).limit(30).stream()]
    treino_plano = next((dict(d.to_dict(), id=d.id) for d in base.collection("treinos")
                         .order_by("criado_em", direction=firestore.Query.DESCENDING).limit(1).stream()), None)
    return jsonify({"uid": uid, "perfil": perfil, "dietas": dietas, "medicoes": medicoes,
                    "consumo": consumo, "treino_plano": treino_plano, "treino_sessoes": treino_sessoes})


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
