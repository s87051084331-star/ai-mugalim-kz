# AI Мұғалім көмекшісі — JSON fix

Түзетілді:
- Gemini/OpenAI жауаптары бір стандартты JSON құрылымына келтіріледі.
- `mode: undefined` жойылды.
- `Cannot read properties of undefined (reading 'tasks')` қорғалды.
- ҚМЖ талданған соң пән, сынып, тақырып, оқу мақсаты, сабақ мақсаты автоматты толтырылады.
- HTML тапсырма жасау алдында tasks бар-жоғы тексеріледі.

Render Environment ішінде `GEMINI_API_KEY` немесе `OPENAI_API_KEY` болуы керек.
