# ZEREK AI Мұғалім көмекшісі — FINAL

Бір пакетте:
- Desktop + responsive Mobile
- PWA / басты экранға орнату
- ZEREK favicon / SEO / sitemap / robots
- Қазақша / Русский / English
- Email тіркелу, admin approval
- Gemini + OpenAI + retry/fallback
- ҚМЖ талдау, ҚМЖ суреттері
- HTML тапсырма, жауап тексеру
- QR қатысу
- сынып / Excel CSV импорт
- сабақ мониторингі
- ауызша жауап
- жеке AI талдау
- журнал
- топқа бөлу әдістері
- топтық жұмыс / экран режимі / сыртқы ресурстар
- PNG / print-PDF
- дизайнерлік парақ
- фото үлгісін Gemini/OpenAI Vision арқылы талдау

Render Environment:
GEMINI_API_KEY, GEMINI_MODEL
OPENAI_API_KEY, OPENAI_MODEL
ADMIN_EMAIL, ADMIN_PASSWORD, SESSION_SECRET

ЕСКЕРТУ: users.db Render Free ephemeral filesystem-де deploy/restart кезінде жоғалуы мүмкін.
Тұрақты пайдаланушы базасы үшін келесі production қадам — PostgreSQL.
