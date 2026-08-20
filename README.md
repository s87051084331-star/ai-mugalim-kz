# ZEREK Education — 3 тіл + рұқсатпен кіру

Қосылды:
- Қазақша / Русский / English тіл ауыстырғыш
- Email + құпиясөз арқылы тіркелу
- Жаңа аккаунт `pending`
- Тек admin мақұлдағаннан кейін кіру
- Admin панелі: рұқсат беру / бас тарту
- Қауіпсіз PBKDF2 пароль хэші және signed session cookie

Render Environment Variables міндетті:
- `ADMIN_EMAIL` = сіздің әкімші email
- `ADMIN_PASSWORD` = әкімші құпиясөзі
- `SESSION_SECRET` = ұзын кездейсоқ құпия жол

Маңызды:
Бұл нұсқа SQLite қолданады. Render-дің ephemeral filesystem режимінде база restart/deploy кезінде жоғалуы мүмкін.
Тұрақты production үшін PostgreSQL/Firebase сияқты тұрақты базаға көшіру керек.
