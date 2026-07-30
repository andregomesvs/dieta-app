# Auditoria e planejamento — Plataforma de acompanhamento de saúde

**Escopo desta entrega:** Sprint 0 — auditoria e planejamento. **Nenhum arquivo de produção foi alterado.**
**Base analisada:** `dieta-app` (app pessoal de dieta com IA + Telegram).
**Data:** conforme sessão.

---

## 1. Inventário técnico (fatos do código)

| Camada | Tecnologia / fato observado |
|---|---|
| Backend | Python + Flask, servido por Gunicorn. **Arquivo único** `backend/app.py` (~1.230 linhas) concentrando config, auth, domínio, prompts, Telegram, cron, admin. |
| Frontend | HTML/CSS/JS **sem framework**. `index.html` (login) + `app.html` (SPA por "views") + `static/css/style.css` + `static/js/firebase-config.js`. Estado em variáveis globais no módulo do `app.html`. |
| Auth | Firebase Authentication (**login Google apenas**). Backend valida ID token (`require_auth`) e admin por e-mail (`require_admin`). |
| Persistência | Cloud Firestore (schemaless). Coleções: `usuarios/{uid}`, subcoleções `dietas`, `medicoes`, `consumo`, `lembretes`; coleção `tg_links`. **Sem migrations.** |
| IA | Google Gemini via SDK **`google-generativeai==0.8.3` (legado/descontinuado)**. Usado em: extração de PDF (bio), extração de exame de sangue, análise de saúde, geração de dieta, estimativa de calorias. |
| Notificações | Bot do Telegram (Bot API) + **webhook** para receber respostas + **cron externo** (cron-job.org) chamando `/cron/send-reminders`. |
| Uploads | Lidos **em memória** e enviados ao Gemini. **Arquivo original NÃO é armazenado.** Limite de 10 MB e checagem de MIME (adicionados recentemente). |
| Testes/CI | **Inexistentes** (sem testes, lint, type checking, CI, staging, observabilidade). |
| Deploy | Render (Web Service único) via `render.yaml`; código no GitHub. Segredos em variáveis de ambiente e Secret File (service account). |
| Dependências | `flask 3.0.3`, `firebase-admin 6.5.0`, `google-generativeai 0.8.3`, `python-dotenv 1.0.1`, `gunicorn 22.0.0`, `tzdata 2024.1`. (flask-cors removido.) |
| Regras Firestore | `firestore.rules` nega acesso direto do cliente (`if false`) — todo acesso passa pelo backend (Admin SDK). |

**Baseline técnico:** não foi possível executar lint/testes/build nesta sessão (ambiente de shell indisponível e projeto sem suíte de testes). A validação foi por **leitura direta** dos arquivos e do mapa de rotas.

---

## 2. Mapa de rotas (backend) e telas (frontend)

**Rotas (confirmadas em `app.py`):**

- Páginas/infra: `/`, `/app`, `/static/<path>`, `/health`, `/models`.
- Perfil/dados: `GET/POST /api/profile`, `GET/POST /api/measurements`, `GET /api/me`.
- IA: `POST /api/analyze-pdf`, `POST /api/analyze-blood`, `POST /api/health-analysis`, `POST /api/generate-diet`, `POST /api/estimate-kcal`.
- Dieta: `GET /api/diets`, `PUT /api/diets/<id>`.
- Consumo/aderência: `GET /api/consumo/today`, `POST /api/consumo/registrar`, `POST /api/consumo/remover`.
- Telegram: `GET /api/telegram/detect`, `POST /api/telegram/test`, `POST /api/telegram/link-code`, `POST /telegram/webhook`, `GET/POST /telegram/setup`, `GET /telegram/info`.
- Cron: `GET/POST /cron/send-reminders` (protegido por `CRON_SECRET`).
- Admin (somente leitura): `GET /api/admin/users`, `GET /api/admin/users/<uid>`.

**Telas (views no `app.html`):** `home` (dashboard), `form` (wizard 4 passos: documentos → dados → objetivo/rotina → Telegram), `dieta` (ver/editar/exportar), `analise` (exames), `medicao` (registrar), `meta`, `admin`. Login em `index.html`.

**Papéis existentes:** apenas **usuário** e **admin** (por `ADMIN_EMAILS`). Não existe personal trainer nem governança.

---

## 3. Arquitetura atual (diagrama textual)

```
[Browser: index.html/app.html (SPA JS)]
        |  Firebase Auth (Google) -> ID token
        v
[Flask app.py em Gunicorn @ Render]  --same-origin, sem CORS--
   |          |                |                 |
   v          v                v                 v
[Firestore] [Gemini API]  [Telegram Bot API]  [cron-job.org -> /cron]
   (Admin SDK)  (síncrono)   (webhook + envio)   (a cada 10 min)
```

Características: monólito síncrono; IA chamada **no request** (sem jobs assíncronos); um único processo web; sem fila; sem storage de arquivos; RBAC binário (user/admin).

---

## 4. Matriz de aderência — requisito vs. implementação atual

Legenda: ✅ Atende · 🟡 Parcial · ❌ Ausente

| Req. | Tema | Status | Evidência / lacuna |
|---|---|---|---|
| RF-01 | Auth e conta | 🟡 | Login Google (Firebase) + verificação de token no backend. **Falta:** e-mail/senha e recuperação, gestão de sessões/dispositivos, exportação/exclusão. |
| RF-02 | Consentimento e privacidade | ❌ | Não há registro de consentimento versionado, granular, nem bloqueio por consentimento ausente/revogado. |
| RF-03 | Cadastro progressivo | 🟡 | Existe wizard de 4 passos (foco em dieta). **Falta:** as 9 etapas de saúde, finalidade por pergunta, "prefiro não informar", retomada explícita, distinção ausência × negativa. |
| RF-04 | Perfil longitudinal | 🟡 | `medicoes` tem data/origem/`fonte`; perfil reflete a medição mais recente. **Falta:** proveniência completa (responsável, declarado/extraído/validado/inferido), trilha ao corrigir, aviso de dado antigo. |
| RF-05 | Hábitos e diários | 🟡 | Aderência alimentar (`consumo`) e medições. **Falta:** sono, álcool, tabagismo, energia, dor, atividade. Linguagem já é não-punitiva. |
| RF-06 | Documentos e medições | 🟡 | Upload PDF/imagem, limite e MIME básicos, extração estruturada (marcadores/bio). **Falta crítica:** o **arquivo original não é armazenado**; sem storage privado/URL temporária; sem verificação de conteúdo malicioso; extração sem unidade/confiança padronizadas nem fluxo formal de confirmação. |
| RF-07 | Elegibilidade baixo risco | ❌ | Não existe motor de regras determinístico, estados, versão nem bloqueio. |
| RF-08 | Relatório sugestivo da IA | 🟡 | Há `health-analysis` (destaques, recomendações, suplementos) e geração de dieta, com avisos. **Falta:** gating por elegibilidade, evidências por ID, schema validável, confiança por achado, declaração de dados insuficientes estruturada. |
| RF-09 | Biblioteca de exercícios | ❌ | Inexistente (produto é dieta, não treino). |
| RF-10 | Rascunho de treino | ❌ | Inexistente. |
| RF-11 | Fila do personal | ❌ | Inexistente. |
| RF-12 | Revisão e aprovação | ❌ | Inexistente (a dieta é editável pelo próprio usuário, não há revisão profissional). |
| RF-13 | Publicação versionada | ❌ | Dietas têm histórico, mas sem fluxo aprovado/publicado por profissional. |
| RF-14 | Execução do treino | 🟡 (análogo) | Existe execução/registro **alimentar** (consumo). Execução de **treino** (carga, RPE, dor) é inexistente. |
| RF-15 | Notificações | 🟡 | Lembretes de refeição (Telegram + cron). **Falta:** preferências, horário silencioso, fuso individual, limite de frequência, templates seguros. |
| RF-16 | Auditoria | ❌ | Não há eventos de auditoria. |
| RNF-01 | Segurança/privacidade | 🟡 | GCP (cripto em trânsito/repouso), segredos em env, authz no backend por `uid`, escape de HTML no render, limites de upload. **Falta:** storage com URL temporária, redação em logs, processo de incidente, verificação anti-IDOR sistemática/testada. |
| RNF-02 | LGPD e governança | ❌ | Sem consentimento, retenção, DSAR, DPIA, contrato/DPA com provedores para dado de saúde. |
| RNF-03 | Acessibilidade | 🟡 | Contraste, alvos e foco melhorados recentemente. **Falta:** ARIA, testes com leitor de tela, conformidade WCAG 2.2 AA verificada. |
| RNF-04 | Performance | 🟡 | APIs simples ok. **Falta:** IA/extração como **jobs assíncronos**, paginação, upload com retomada. |
| RNF-05 | Confiabilidade | 🟡 | Cron isolado por usuário; marcadores por dieta+refeição. **Falta:** idempotência transacional, retry/backoff, dead-letter, monitoramento. |
| RNF-06 | Qualidade | ❌ | Sem tipagem, lint, testes, migrations, feature flags. |

**Síntese:** o app cobre bem um **produto pessoal de dieta**. Para a **plataforma de saúde com revisão profissional**, o núcleo novo (consentimento, elegibilidade, RBAC multi-papel, biblioteca/treino, revisão/aprovação, auditoria, LGPD, testes, jobs) é majoritariamente **greenfield**. Aproveitável hoje: auth, upload+extração, dados longitudinais (medições), relatório sugestivo da IA e notificações — todos precisam de endurecimento.

---

## 5. Riscos e bloqueadores

**Bloqueadores estruturais (decidir antes de codar o núcleo):**

1. **Banco de dados.** Firestore é schemaless e o domínio-alvo exige integridade referencial forte, versionamento, estados validados e **auditoria imutável**. Decisão: manter Firestore (velocidade/compatibilidade, com camada de repositório + validação por schema em código) **ou** introduzir Postgres para o novo domínio clínico. Recomendação: **Firestore com repositório+schemas** no MVP; reavaliar Postgres se relatórios/integridade exigirem.
2. **Infra de jobs assíncronos.** RF-08/RNF-04 pedem geração de IA e extração assíncronas com retry/dead-letter. Render não traz fila nativa. Opções: coleção `jobs` processada por worker acionado pelo cron (padrão já usado), Cloud Tasks, ou serviço worker separado. Precisa de decisão.
3. **Storage de arquivos.** RF-06 exige preservar o original em storage privado com URL temporária — **hoje não guardamos arquivo nenhum**. Introduzir Firebase Storage (mesmo projeto) é o caminho compatível.
4. **SDK de IA descontinuado.** `google-generativeai` está deprecado; migrar para `google-genai` antes de investir no contrato de IA versionado.
5. **Monólito.** `app.py` e `app.html` únicos elevam risco de regressão à medida que o domínio cresce; modularizar cedo (repositórios, serviços, schemas) reduz custo.

**Riscos de produto/clínico/jurídico (não são decisão de engenharia):**

6. **Critérios clínicos de elegibilidade e motivos de bloqueio** — precisam ser definidos e validados por profissionais qualificados/governança. Não serão inventados; entram como **configuração versionada + feature flag**.
7. **Envio de dados de saúde ao Gemini** exige base legal, consentimento específico e DPA/contrato — revisão jurídica antes de produção.
8. **Credenciamento do personal trainer** (verificação de identidade/registro) — processo de negócio a definir.
9. **Limiares de "dado atual" (staleness), sinais de suspensão (dor), contraindicações de exercícios** — definição clínica/governança.

---

## 6. Arquitetura-alvo incremental (compatível com a stack)

Manter Flask + Firestore + Gemini + SPA JS, evoluindo o monólito para módulos:

```
backend/
  app.py                # apenas compõe a aplicação e registra blueprints
  config.py             # env, feature flags, versões de política
  auth/                 # verificação de token, RBAC (user/personal/admin/governanca)
  repos/                # acesso a Firestore por entidade (IDs opacos)
  schemas/              # validação (pydantic) de perfil, dieta, consumo, IA, treino
  domain/
    eligibility/        # motor de regras determinístico, versionado, com motivos
    ai/                 # contrato: pacote minimizado, schema de saída, guardrails, mock
    training/           # biblioteca, rascunho, revisão, publicação, sessões
    audit/              # AuditEvent append-only
  jobs/                 # fila em coleção 'jobs' + worker acionado por cron
  telegram/, cron/      # já existentes, reaproveitados
frontend/
  componentes reutilizáveis (page-header, button, badge, card, upload-tile,
  segmented-control, metric, field-help, field-error) para eliminar estilo inline
```

Princípios de execução: RBAC **no backend**; transições de estado (caso de saúde / plano de treino) validadas no servidor; IA sempre sugestiva e schema-validada; auditoria em coleção protegida; feature flags para liberar gradual; **preservar o módulo de dieta** intacto.

---

## 7. Backlog reestimado (sprints de 2 dias)

O roadmap do documento (21 sprints) é uma boa espinha dorsal. Reestimativa após auditoria, marcando o que já existe:

| Sprint | Objetivo | Ajuste após auditoria |
|---|---|---|
| 0 | Auditoria e baseline | **Este documento.** Concluído (sem suíte de testes a rodar). |
| 1 | Fundação: modularizar + feature flags + estados + migrar `google-genai` | Novo; alto valor para reduzir risco antes de crescer. |
| 2 | RBAC no backend (user/personal/admin/governança) + testes de isolamento | Hoje só user/admin; expandir e testar IDOR. |
| 3 | Consentimento versionado + bloqueios | Greenfield (RF-02). |
| 4 | Cadastro progressivo de saúde (9 etapas) + retomada | Reusar wizard atual como base (RF-03). |
| 5 | Perfil longitudinal + proveniência | Estender `medicoes`/perfil (RF-04). |
| 6 | Hábitos/diários (sono, álcool, tabaco, energia, dor) | Reusar padrão do `consumo` (RF-05). |
| 7 | Upload seguro + **Firebase Storage** (preservar original) | Fechar lacuna crítica do RF-06. |
| 8 | Extração estruturada + confiança + confirmação | Estender extração atual (RF-06). |
| 9 | **Motor de elegibilidade** versionado + bloqueio | Greenfield, depende de governança (RF-07). |
| 10 | Contrato de IA: schema, pacote minimizado, mock determinístico, guardrails | Reescrever camada de IA atual (RF-08/6.x). |
| 11 | Relatório sugestivo assíncrono + evidências + lacunas | Evoluir `health-analysis` (RF-08). |
| 12 | Biblioteca de exercícios (CRUD interno, versões) | Greenfield (RF-09). |
| 13 | Rascunho de treino | Greenfield (RF-10). |
| 14 | Painel do personal (fila, atribuição, resumo) | Greenfield (RF-11). |
| 15 | Revisão e aprovação (diff, checklist, versão) | Greenfield (RF-12). |
| 16 | Publicação + acompanhamento ao usuário | Greenfield (RF-13). |
| 17 | Registro da sessão de treino (carga, RPE, dor, suspensão) | Análogo ao consumo (RF-14). |
| 18 | Notificações (preferências, fuso, horário silencioso, templates) | Endurecer Telegram + canais (RF-15). |
| 19 | Auditoria + LGPD (export/exclusão/retenção) | Greenfield (RF-16/RNF-02). |
| 20 | Hardening (segurança, performance, acessibilidade, observabilidade) | Consolidação. |
| 21 | Piloto controlado com dados sintéticos + go/no-go | Gate final. |

**Marco de fundação segura:** fim do Sprint 11 (consentimento + elegibilidade + contrato de IA sugestiva). **Marco de plataforma de treino revisada:** fim do Sprint 17.

---

## 8. Decisões que bloqueiam a continuidade (precisam de validação)

Estas não são de engenharia; sem elas, os sprints correspondentes ficam com placeholder/feature flag:

1. **Critérios de elegibilidade de baixo risco e motivos de bloqueio** (governança/clínico) — define RF-07 e o gating de tudo.
2. **Base legal + consentimento + DPA para enviar dado de saúde ao Gemini** (jurídico) — define RF-02/RF-08/RNF-02.
3. **Banco de dados do novo domínio:** Firestore (recomendado) vs Postgres — define a fundação.
4. **Infra de jobs assíncronos** (coleção+worker via cron vs Cloud Tasks) — define IA/extração.
5. **Guardar o arquivo original** em Firebase Storage (privacidade) — define RF-06.
6. **Modelo de credenciamento do personal trainer** — define RF-11/RF-12.
7. **Conteúdo e contraindicações da biblioteca de exercícios** (governança) — define RF-09/RF-10.
8. **Limiares de dado "atual" e sinais de suspensão** (clínico) — define RF-04/RF-14.

> Recomendação de menor mudança segura para começar sem esperar tudo: **Sprint 1 (modularização + migrar `google-genai` + feature flags) e Sprint 2 (RBAC + isolamento testado)** não dependem de decisão clínica e reduzem risco imediatamente. As decisões 1, 2 e 7 devem ser encaminhadas em paralelo por serem pré-requisito do núcleo clínico.
