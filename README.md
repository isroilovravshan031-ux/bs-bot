# Nakladnoy → 1C CSV bot

Qo'lda yozilgan nakladnoy rasmini Telegram'ga yuborasiz, Gemini o'qiydi, siz tekshirib tasdiqlaysiz, kun oxirida bitta CSV olasiz.

Python · PostgreSQL · Gemini API · aiogram

## Ishga tushirish

1. **Bot token:** Telegram'da @BotFather → `/newbot`.
2. **Gemini kaliti:** https://aistudio.google.com/apikey
3. `.env` faylini oching va to'ldiring (`DATABASE_URL` allaqachon yozilgan):
   ```
   TELEGRAM_BOT_TOKEN=...
   GEMINI_API_KEY=...
   ALLOWED_USER_IDS=sizning_telegram_id
   ```
   ID'ingizni bilmasangiz, avval bo'sh qoldirib ishga tushiring va botga `/start` yozing: bot ko'rsatadi.
4. Tekshirish va ishga tushirish:
   ```
   .venv\Scripts\python -m nakladnoy_bot doctor
   .venv\Scripts\python -m nakladnoy_bot read samples\nakladnoy-2026-11-03.jpg
   powershell -ExecutionPolicy Bypass -File scripts\start.ps1
   ```

Windows'ga kirganda o'zi yoqilishi uchun: `scripts\install-autostart.ps1`. Bot kompyuter yoniq va internet bor paytda ishlaydi; o'chiq paytda yuborilgan rasmlar yoqilganda o'qiladi.

Noldan o'rnatish (boshqa kompyuterda): `scripts\setup.ps1`. PostgreSQL bo'lmasa winget bilan o'rnatadi, botga alohida klaster yaratadi (port 5433, asosiy 5432 serverga tegmaydi).

## Railway'da ishga tushirish

Kompyuter o'chiq bo'lsa ham bot ishlashi uchun. Repo'da `Dockerfile` va `railway.json` bor, Railway ularni o'zi topadi.

1. Railway → **New Project → Deploy from GitHub repo** → shu repo.
2. Loyihada **+ New → Database → PostgreSQL**.
3. Bot servisi → **Variables**:

   | O'zgaruvchi | Qiymat |
   |---|---|
   | `DATABASE_URL` | `${{Postgres.DATABASE_URL}}` |
   | `TELEGRAM_BOT_TOKEN` | BotFather tokeni |
   | `GEMINI_API_KEY` | Gemini kaliti |
   | `GROQ_API_KEY` | ixtiyoriy zaxira |
   | `ALLOWED_USER_IDS` | Telegram ID'lar, vergul bilan |

4. Deploy tugagach, **Logs** da `Run polling for bot` chiqishi kerak.
5. Kontragentlar: repo ochiq bo'lgani uchun haqiqiy ro'yxat unda yo'q. Botga `/kontragent_import` yozib, `ИНН;Контрагент;Договор` ustunli CSV yuboring.

**Muhim:**
- Bitta token bilan faqat **bitta** bot ishlashi mumkin. Railway'dagisi yoqilganda kompyuterdagini to'xtating, aks holda Telegram `Conflict` xatosini beradi va xabarlar ikkalasiga bo'linadi.
- Rasmlar bazada saqlanadi (`document_images`), shuning uchun Railway deploy'da diskni tozalasa ham «Qayta o'qish» ishlaydi. Volume kerak emas.
- Yangi versiya deploy bo'lganda u eski nusxa to'xtashini kutadi (bazadagi qulf), ikkalasi bir vaqtda xabar o'qimaydi.

## Kundalik ish

| Qadam | Nima bo'ladi |
|---|---|
| Rasm yuborasiz (bir nechtasini ham) | Har biri alohida karta bo'lib chiqadi |
| Kartani rasm bilan solishtirasiz | ⛔ tasdiqlashni to'sadi, ⚠️ faqat ogohlantiradi |
| **✅ Tasdiqlash** | Hujjat navbatga tushadi, nomlar keyingi safar uchun o'rganiladi |
| **📄 CSV olish** | Navbatdagi hammasi bitta faylga tushadi va qayta chiqmaydi |
| `/tarix` | Oldingi faylni qayta yuklab olish |

Yozuv xira bo'lsa, rasmni **fayl sifatida** yuboring: Telegram siqmaydi.

## Xatodan himoya

- **Hisob-kitob:** soni × narxi = summa va qatorlar yig'indisi = jami tekshiriladi. Mos kelmasa, AI aynan shu kataklarni bir marta qayta o'qiydi. Baribir mos kelmasa, qog'ozdagi raqam jim almashtirilmaydi, kartada ⚠️ chiqadi.
- **Ikki marta kirish:** bir xil rasm qayta o'qilmaydi. Boshqa rasm, lekin shu kontragent, sana va summa bo'lsa ⚠️. Eksport qilingan hujjat qulflanadi.
- **Kontragent:** INN, o'rganilgan yozuv, aniq nom, taxminiy moslik tartibida topiladi (kirill/lotin farqi hisobga olinmaydi). Ishonchli topilmasa, tanlamaguncha tasdiqlab bo'lmaydi.
- **Kirish:** faqat `ALLOWED_USER_IDS`. Rasmdagi matn AI uchun buyruq emas, ma'lumot; AI'da hech qanday asbob yo'q.

## CSV shakli

`config/export_profile.toml` da: kodga tegmasdan ustun nomi, tartibi, ajratkich, kodirovka o'zgaradi (o'zgartirgach botni qayta yoqing).

- Bir material = bir qator. Ustunlar 1C skrinshotidagi tartibda, oxirida tarkib: `Ключ документа; № строки; Материал; Ед.изм.; Количество; Цена; Сумма`.
- `Ключ документа` (NK-000012) bitta hujjat qatorlarini guruhlaydi.
- Kodirovka UTF-8 BOM. `cp1251` ishlamaydi: nomlarda `Қ`, `ғ`, `қ` bor. Tanlansa, eksport buzilgan fayl chiqarish o'rniga to'xtaydi.

### Tasdiqlanishi kerak

| Joy | Holat |
|---|---|
| `export_profile.toml`: `department` | Skrinshotda `склад снаб...` deb kesilgan, `склад снабжения` taxmin |
| `export_profile.toml`: `responsible` | Skrinshotda kesilgan, bo'sh qoldirilgan |
| Ikki kontragent shartnomasi | Skrinshotda sana kesilgan, yili raqamdan taxmin. Tuzatish: `/kontragent_qosh` |
| Material nomlari | 1C katalogi yo'q. `/material_import` bilan yuklang, aks holda qog'ozdagi nom CSV'ga tushadi |

## 1C'ga yuklash

CSV'ni o'qiydigan vosita 1C tomonda bo'lishi kerak. Agar konfiguratsiyada yuklash obrabotkasi bo'lsa, ustunlarini `export_profile.toml` ga moslang. Bo'lmasa: `1c/ZagruzkaPostupleniya.bsl` da qoralama bor. U hujjatni provodkasiz yaratadi va qayta yuklashda ikkilamaydi, lekin **1C'da sinalmagan** va metadata nomlari taxmin; 1C dasturchisi test bazada moslashi kerak.

## AI provayderlari

O'qish tartibi: `gemini-3.8-flash` → `gemini-3.6-flash` → `gemini-3.5-flash-lite` → Groq `qwen/qwen3.8-27b` (`.env` da o'zgaradi).

- **Groq** faqat Gemini'ning hamma modeli ishlamaganda ishlatiladi: limit, server xatosi yoki kalit xatosi. `GROQ_API_KEY` bo'sh bo'lsa, zaxira o'chiq.
- **Groq aniqligi:** sinovdagi nakladnoyda barcha raqamlar to'g'ri, lekin qo'lyozma nomlar xato bo'ldi ("Офис папка" → "Орис Палка"). Shuning uchun kartada doim ogohlantirish chiqadi; nomlarni tuzatib tasdiqlang.
- **Groq limiti:** bepul tarifda daqiqasiga 7 000 kirish tokeni, bitta rasm taxminan 2 400 token, ya'ni daqiqasiga 2–3 ta rasm. Limit tugasa, bot server aytgan vaqtni (30 soniyagacha) kutadi.
- **Groq PDF o'qimaydi**, faqat rasm.
- Har bir provayderni alohida sinash: `python -m nakladnoy_bot read rasm.jpg --provider groq`
- **Groq maxfiyligi:** hujjatiga ko'ra so'rovlarni standart holatda saqlamaydi, lekin nosozlik yoki suiiste'mol tekshiruvi uchun 30 kungacha log qilishi mumkin. Data Controls sozlamasida o'chiriladi.

- **Gemini maxfiyligi:** Google narx sahifasiga ko'ra bepul tarif ma'lumoti mahsulotni yaxshilashda ishlatiladi, pullik tarifniki ishlatilmaydi. Nakladnoylarda INN va narxlar bor, shuning uchun kundalik ishda pullik kalit tavsiya etiladi.
- Sarf `ai_calls` jadvalida yoziladi.

## Texnik

```
nakladnoy_bot/
  handlers.py    Telegram tugma va xabarlari
  service.py     Biznes mantiq (o'qish, tahrir, tasdiq, eksport)
  gemini.py      AI so'rovi, qayta urinish, zaxira model
  validation.py  Tekshiruvlar
  matching.py    Kontragent va material moslash
  exporter.py    CSV
migrations/      SQL (ishga tushganda o'zi qo'llanadi)
```

Testlar (haqiqiy PostgreSQL, Telegram va Gemini tarmog'isiz):

```
.venv\Scripts\python -m pytest
```

Bazani to'xtatish: `"C:\Program Files\PostgreSQL\18\bin\pg_ctl.exe" stop -D .pg\data`
