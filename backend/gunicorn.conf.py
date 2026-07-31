# Gunicorn carrega este arquivo automaticamente quando roda em backend/ (rootDir).
# Garante concorrência mesmo se o startCommand antigo (sem --threads) continuar ativo:
# sem threads, uma chamada lenta ao Gemini ("Estimar") prende o worker e o "Salvar"
# fica na fila. Com threads, o "Salvar" é atendido em paralelo.
import os

bind = f"0.0.0.0:{os.environ.get('PORT', '10000')}"
workers = 1
threads = 8
timeout = 120
graceful_timeout = 30
