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
from flask_cors import CORS

import firebase_admin
from firebase_admin import credentials, auth as fb_auth, firestore

import google.generativeai as genai

load_dotenv()

# ---------------------------------------------------------------------------
# Configuração
# ---------------------------------------------------------------------------
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
CRED_PATH = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "./firebase-service-account.json")
FIREBASE_PROJECT_ID = os.environ.get("FIREBASE_PROJECT_ID", "")

# Telegram + agendamento de lembretes
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CRON_SECRET = os.environ.get("CRON_SECRET", "")
TIMEZONE = os.environ.get("TIMEZONE", "America/Sao_Paulo")
# Tolerância (min): manda a refeição se o horário caiu nos últimos N minutos.
REMINDER_WINDOW_MIN = int(os.environ.get("REMINDER_WINDOW_MIN", "15"))

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


def tg_send(chat_id, text):
    return tg_call("sendMessage", {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    })


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
    pdf_bytes = None

    if "file" in request.files:
        pdf_bytes = request.files["file"].read()
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
    media = _ALLOWED_MEDIA.get((f.mimetype or "").lower(), "application/pdf")
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
  "aviso": "Interpretação gerada por IA. Leve seus exames a um médico ou nutricionista."
}

Ordene os destaques do mais importante para o menos. Priorize itens com status \
'alerta' e 'atencao'. Foque no que dá para melhorar pela alimentação."""


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

    user_msg = f"""Dados do usuário:
{json.dumps(perfil, ensure_ascii=False, indent=2)}

Objetivo: {objetivo}
Prazo do objetivo: {prazo}
Rotina de treino (com horários): {rotina_treino}
Rotina alimentar atual e horários: {rotina_alimentar}
Restrições / preferências alimentares: {restricoes or "nenhuma informada"}

Monte a dieta seguindo o formato JSON solicitado."""

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
    reg = {"data": data.get("data") or datetime.date.today().isoformat()}
    for c in MEDICAO_CAMPOS:
        v = data.get(c)
        if v is not None and v != "":
            try:
                reg[c] = float(v)
            except (TypeError, ValueError):
                pass
    reg["criado_em"] = datetime.datetime.utcnow().isoformat()
    ref = db().collection("usuarios").document(g.uid).collection("medicoes").add(reg)
    reg["id"] = ref[1].id

    # mantém o perfil com os valores mais recentes
    perfil_update = {c: reg[c] for c in MEDICAO_CAMPOS if c in reg}
    if perfil_update:
        db().collection("usuarios").document(g.uid).set(perfil_update, merge=True)
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

    for user_doc in db().collection("usuarios").stream():
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
        dieta = (dietas[0].to_dict() or {}).get("dieta") or {}
        refeicoes = dieta.get("refeicoes") or []

        # marcador de enviados do dia
        marca_ref = (db().collection("usuarios").document(user_doc.id)
                     .collection("lembretes").document(hoje))
        ja_enviados = set((marca_ref.get().to_dict() or {}).get("enviados", []))

        for idx, ref in enumerate(refeicoes):
            hm = parse_horario(ref.get("horario"))
            if not hm:
                continue
            alvo = agora.replace(hour=hm[0], minute=hm[1], second=0, microsecond=0)
            delta_min = (agora - alvo).total_seconds() / 60.0
            chave = f"{idx}"
            if 0 <= delta_min <= REMINDER_WINDOW_MIN and chave not in ja_enviados:
                try:
                    tg_send(chat_id, format_meal_message(ref))
                    ja_enviados.add(chave)
                    enviados_total += 1
                    detalhes.append({"uid": user_doc.id, "refeicao": ref.get("nome")})
                except Exception as e:  # noqa: BLE001
                    detalhes.append({"uid": user_doc.id, "erro": str(e)})

        marca_ref.set({"enviados": list(ja_enviados)}, merge=True)

    return jsonify({"hora": agora.isoformat(), "enviados": enviados_total, "detalhes": detalhes})


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
