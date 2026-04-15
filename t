========================================================================================================================
PRE-HISTORY:

Так, саме так. Стандартний flow:

  ~/.claude/               # ОДНА директорія на весь Claude Code
  ├── .claude.json         # oauthAccount (active identity)
  ├── .credentials.json    # OAuth tokens (macOS: в Keychain
  замість цього)
  ├── history.jsonl        # ВСІ розмови
  ├── projects/            # ВСІ projects
  ├── agent-memory/        # ВСЯ пам'ять
  ├── commands/            # команди
  └── settings.json        # preferences

  Коли ти /login вдруге іншим email — перезаписуються тільки
  OAuth tokens в Keychain + oauthAccount в .claude.json. Все інше
   залишається. Але ти можеш бути тільки одним акаунтом
  одночасно.

  Що потребує ізоляції

  Для твого N=20 паралельних сесій тільки credentials мусять
  різнитись — бо CLI hashes Keychain entry по
  sha256(CLAUDE_CONFIG_DIR). Тобто щоб мати 20 одночасних OAuth
  tokens, треба 20 різних CLAUDE_CONFIG_DIR шляхів (саме це
  робить CCSwitch).

  Все інше (історія, projects, agent-memory, plugins, commands,
  settings) ділити можна.

  Пропозиція: мінімалістичні per-account stubs

  ~/.claude/                                     # SHARED, 9 GB,
  єдина копія
  ├── agent-memory/
  ├── commands/
  ├── plugins/
  ├── settings.json
  ├── CLAUDE.md
  ├── history.jsonl           # всі розмови разом, незалежно від
  акаунта
  ├── projects/
  └── file-history/

  ~/.ccswitch-accounts/account-<hash>/           # ~25 KB stub на
   акаунт
  ├── .claude.json            # ТІЛЬКИ oauthAccount + userID
  (per-account)
  ├── agent-memory/     → ../../.claude/agent-memory
  ├── commands/         → ../../.claude/commands
  ├── plugins/          → ../../.claude/plugins
  ├── settings.json     → ../../.claude/settings.json
  ├── CLAUDE.md         → ../../.claude/CLAUDE.md
  ├── history.jsonl     → ../../.claude/history.jsonl
  ├── projects/         → ../../.claude/projects
  └── file-history/     → ../../.claude/file-history

  Коли твоя cmux сесія експортує
  CLAUDE_CONFIG_DIR=~/.ccswitch-accounts/account-abc/, CLI:
  - Читає .claude.json → бачить акаунт abc → використовує
  Keychain entry для sha256(~/.ccswitch-accounts/account-abc)
  - Пише history в symlink → все йде в shared
  ~/.claude/history.jsonl
  - Читає agent-memory через symlink → бачить твою спільну
  пам'ять
  - Налаштування, плагіни, команди — spільні

  Наслідки

  ✅ 9 GB data stored once, не × 15 акаунтів
  ✅ Спільна agent-memory для всіх акаунтів — твої інструменти
  бачать один mental model
  ✅ Всі projects доступні з будь-якого акаунта — не губиться
  контекст при switch
  ⚠️ history.jsonl змішаний — розмови всіх акаунтів в одному
  файлі (якщо ти дивишся історію — побачиш все разом, без "це
  було на біз акаунті")
  ⚠️ Одночасний запис в history.jsonl з 20 CLI — треба перевірити
   що Claude Code робить append'и атомарно. Якщо ні — corruption
  risk

  Що це НЕ вирішує

  - Refresh race між CCSwitch і CLI — окрема задача (W / W+ / A3
  все ще актуальні)
  - Consolidation зменшує storage + split-brain, але race
  залишається той самий

  Питання

  Показую підхід, ок? Хочеш я складу:
  1. Детальний migration script (з dry-run) який:
    - Почистить дублікати всередині ~/.claude-accounts/
  (leusnazarii/ + leusnazarii.biz/)
    - Перенесе ~/.claude-accounts/nazariileus/* в чистий
  ~/.claude/ (після розірвання симлінка)
    - Додасть symlink-farm у всі 15
  ~/.ccswitch-accounts/account-<hash>/
    - Orphan Keychain entries підчистить
  2. Після цього повернутися до W/W+ вибору

  Погоджуєшся на підхід?

✻ Sautéed for 1m 14s · 4 shells still running

❯ Та ні, давай ще простіше. Чому CCSwitch не може
  використовувати одну папку? Для чого створювати папки взагалі?
   Чому не можна використовувати одну стандартну директорію і не
   засмічувати простір?
  ⎿  UserPromptSubmit hook error                                    ⎿  Failed with non-blocking status code: Error: LETTA_API_KEY
                                                                       environment variable is not set



⏺ Правильне питання. Відповідь залежить від того що тобі реально
  треба:

  Фундаментальне обмеження

  Claude Code CLI хешує Keychain entry за шляхом
  CLAUDE_CONFIG_DIR:
  Keychain entry name = "Claude Code-credentials-" +
  sha256(CLAUDE_CONFIG_DIR)[:8]

  Тобто щоб мати N паралельних токенів одночасно — потрібно N
  різних шляхів. Це єдиний спосіб дати CLI "20 identities at
  once" без модифікації самого CLI.

  Ключове питання до тебе

  Тобі реально треба 20 акаунтів АКТИВНИХ одночасно в різних
  терміналах? Чи достатньо "один активний, CCSwitch rotate'ить
  коли hit limit"?

  Варіант A: Достатньо one-at-a-time

  Якщо ти ОК з тим що всі твої cmux панелі одночасно працюють на
  одному акаунті, і CCSwitch rotate'ить цей акаунт коли hit
  limit:

  ~/.claude/               # ОДНА директорія, стандарт
  ├── .claude.json         # CCSwitch swap'ить oauthAccount на
  switch
  ├── history.jsonl
  ├── agent-memory/
  └── ...

  Keychain:
  Claude Code-credentials  # CCSwitch swap'ить tokens на switch

  CCSwitch зберігає credential'и всіх акаунтів у власному
  вбудованому vault (наприклад, Keychain entries
  ccswitch-vault-leusnazarii, ccswitch-vault-leusnazarii.biz,
  etc.). На switch: читає токени з vault → записує в стандартний
  Claude Code-credentials + оновлює ~/.claude/.claude.json
  oauthAccount.

  Жодних ccswitch-accounts/ папок. Нуль clutter.

  Але є жорсткий compromise: всі 20 cmux панелей ділять один
  акаунт одночасно. Якщо всі активно працюють — вони hit rate
  limit 20× швидше. Тобто переваги "20 акаунтів = 20× capacity"
  немає. Це послідовне використання 20 акаунтів, не паралельне.

  Варіант B: Треба паралельно (20 акаунтів одночасно працюють)

  Тоді мусиш мати 20 різних CLAUDE_CONFIG_DIR шляхів. Але можемо
  зробити їх мінімалістичними symlink-stub'ами ~25 KB кожен:

  ~/.ccswitch-accounts/account-<hash>/   # 25 KB stub
  └── .claude.json → symlink? чи маленька копія?

  Плюс всі інші файли symlink'ані на ~/.claude/. Це не 15 папок з
   9GB кожна, а 15 папок по 25KB. Візуально 15 директорій, але
  storage майже нуль.

  Питання

  Опиши чесно свій workflow:

  1. Ти в cmux панелях реально хочеш 20 акаунтів одночасно
  активних (щоб мати 20× capacity паралельно)?
  2. Чи тобі ОК щоб було "1 активний акаунт за раз, CCSwitch
  rotate'ить" (послідовно, не паралельно)?

  Якщо (1) — ARCH B (мінімалістичні stubs, 25KB).
  Якщо (2) — ARCH A, жодних папок, чистий ~/.claude/.

  Від цієї відповіді залежить вся подальша архітектура. Цей вибір
   критичний — не пропущу його.

✻ Brewed for 2m 17s · 4 shells still running

❯ Я використовую 1 аккаунт на всіх 20 терміналах. Мені лише
  треба робити новий /login коли досягаємо ліміту
  ⎿  UserPromptSubmit hook error                                    ⎿  Failed with non-blocking status code: Error: LETTA_API_KEY
                                                                       environment variable is not set



⏺ ЦЕ КРИТИЧНО. Твій workflow не потребує паралельних акаунтів
  взагалі. Просто автоматизований /login. Це радикально інша
  архітектура.

  Що ти реально робиш

  20 cmux панелей → всі читають ~/.claude/ (Keychain entry
  "Claude Code-credentials")
                  → всі використовують ОДИН активний акаунт
  Hit limit → ти вручну /login → Keychain entry переписаний
  20 панелей → тепер всі на новому акаунті

  CCSwitch мусить автоматизувати цей /login-swap, не створювати
  паралельні світи.

  Радикально спрощена архітектура

  ~/.claude/                               # ЄДИНА директорія
  ├── .claude.json                         # CCSwitch пише
  oauthAccount на swap
  ├── history.jsonl, agent-memory/, ...    # просто твої дані
  └── (жодних CLAUDE_CONFIG_DIR)

  Keychain:
  ├── Claude Code-credentials              # ONE активний токен
  набір (CLI refresh'ить)
  ├── ccswitch-vault-leusnazarii@...       # CCSwitch'ів vault
  (приватний)
  ├── ccswitch-vault-nazariileus@...       #
  ├── ccswitch-vault-leusnazarii.biz@...
  └── ... (N vault entries, один на акаунт)

  Що робить CCSwitch

  1. Vault — зберігає refresh_token + access_token кожного N
  акаунта в власних Keychain entries ccswitch-vault-<email>
  2. Usage probe — періодично робить /v1/messages з поточним
  активним token'ом, читає rate-limit headers
  3. Swap — коли hit threshold:
    - Читає vault entry для наступного акаунта
    - Атомарно перезаписує стандартний Claude Code-credentials
  Keychain entry
    - Оновлює ~/.claude/.claude.json oauthAccount + userID
    - (Опціонально) tmux nudge щоб 20 панелей "помітили" новий
  акаунт
  4. Refresh — CCSwitch НЕ refresh'ить нічого. CLI сам
  refresh'ить активний акаунт. Upstream 2.1.101 fix координує між
   20 CLI на одному entry.

  Що видаляємо

  - ❌ Папки ~/.ccswitch-accounts/account-<hash>/ — всі 15
  - ❌ Папки ~/.claude-accounts/* — твоя стара симлінк-система
  - ❌ Симлінк ~/.claude → ~/.claude-accounts/nazariileus
  - ❌ Вся логіка CLAUDE_CONFIG_DIR management в CCSwitch
  - ❌ Per-config-dir hashed Keychain entries (orphan cleanup)
  - ❌ credential_targets / mirror infrastructure

  Race analysis (новий)

  - Всі 20 CLI ділять один Keychain entry → intra-CLI race
  handled by upstream 2.1.101 fix
  - CCSwitch не refresh'ить → немає CLI↔CCSwitch race під час
  нормальної роботи
  - Під час swap — теоретично можливий race: CCSwitch пише токени
   акаунта B, в цей же момент CLI refresh'ить акаунт A → CLI
  clobber'ить swap
    - Mitigation: CCSwitch детектує clobber на наступному probe
  (бачить email акаунта A, не B) → re-swap
    - Worst case: перший switch не застосувався, CCSwitch
  автоматично retry'є. Recoverable. No cascade.

  Залишається одна тонкість

  Коли swap трапляється, running CLI в 20 панелях мають в пам'яті
   старі токени. Питання:
  - Якщо CLI на наступному запиті просто робить HTTP call →
  отримує результат за старим access_token → OK (token ще
  валідний)
  - Якщо CLI hit'ає token expiry → намагається refresh →
  використовує старий refresh_token з пам'яті, а не з Keychain?
  Якщо так — refresh проходить за старим акаунтом, clobber'ить
  Keychain
  - Потрібно перевірити empirically чи CLI кешує refresh_token в
  пам'яті чи завжди читає Keychain

  Підтверди

  - ✅ Всі 20 терміналів на одному акаунті одночасно —
  підтверджено
  - CCSwitch → "programmatic /login" = правильна модель?
  - OK видалити всю multi-dir інфраструктуру (всі 15
  ccswitch-accounts/ + .claude-accounts/ + симлінк)?

  Якщо так — пишу новий design doc (короткий цього разу) і план
  міграції. Це буде ~60-70% delete з codebase + ~150 LOC add для
  vault swap.

✻ Sautéed for 1m 40s · 4 shells still running

❯ це кращий підхід? чому інші так не роблять?
  ⎿  UserPromptSubmit hook error                                    ⎿  Failed with non-blocking status code: Error: LETTA_API_KEY
                                                                       environment variable is not set



⏺ Чесна відповідь — так, краще для твого workflow, і ніхто не
  робить тому що workflow нестандартний.

  Чому інші так не роблять

  Multi-account tools майже всі припускають ПАРАЛЕЛЬНЕ
  використання, не серіалізоване:

  - Teams/enterprise сценарії — 20 акаунтів на 20 людей, кожен
  свій, паралельно
  - Per-project isolation — один розробник, але кожен проект
  окремий акаунт з окремою conversation history
  - Load balancing — 20 акаунтів для 20× capacity паралельно (не
  послідовно)

  Твій кейс — один розробник, один проект/workflow, багато
  підписок для extending rate-limit у часі — rare. Більшість OSS
  tools не оптимізують під нього.

  Історично CCSwitch був побудований з припущенням паралелізму.
  Тому per-account directories, тому active-ownership refresh,
  тому вся ця складність. Припущення було хибним для твого
  реального workflow. Коли архітектор обирає метафору "багато
  акаунтів" — інстинктивно думає "паралельно, ізольовано, кожен
  свій світ." Але твій справжній кейс це "один світ, чергування
  облікових даних."

  Інші проекти які я research'ив:
  - Claude Code Router — проксі + API keys, обходить OAuth
  взагалі
  - claude-multisession — per-account dirs (паралельна модель)
  - CCSwitch — per-account dirs + active-ownership (паралельна
  модель)
  - Anthropic офіційно — нічого, Issue #24798 відкрита без
  рішення

  Жоден не обрав "ONE folder + programmatic /login" тому що:
  1. Нестандартна mental model (інтуїтивно multi-account =
  паралельно)
  2. Потребує emipричної перевірки поведінки CLI з Keychain cache
  3. Працює лише для специфічного workflow (твого)
  4. Блокує можливість майбутнього паралелізму

  Чи це "краще"?

  Для ТВОГО workflow (серіалізоване, 20 панелей один акаунт) —
  так, значно.
  - Набагато простіше (~60-70% codebase видалити)
  - Race на refresh неможлива за дизайном (CCSwitch ніколи не
  refresh'ить)
  - Hard boundaries всі поважені (no MITM, no trickery, no
  wrappers)
  - Чистий storage, zero clutter
  - Офіційні API surfaces тільки (Keychain через security CLI,
  .claude.json — де CLI сам його читає)

  Для абстрактного multi-account користувача — ні, бо втрачає
  паралелізм. Але тебе це не стосується.

  Єдиний реальний ризик — емпіричне питання

  Чи Claude Code CLI кешує tokens в process memory, чи на кожен
  API call перечитує Keychain?

  - Якщо перечитує Keychain кожен раз → swap propagate миттєво на
   всі 20 CLI → ідеально
  - Якщо кешує → running CLIs залишаються на старому акаунті поки
   token не протухне → eventual consistency (секунди-хвилини)
  - Якщо кешує дуже жорстко → swap не propagate поки не
  перезапустити CLI

  Upstream 2.1.101 fix ("concurrent sessions re-authentication")
  натякає що CLI таки шарить Keychain state між sessions — інакше
   fix не мав би сенсу. Тому перший варіант найімовірніший. Але
  треба перевірити.

  Пропозиція

  1. Empirical test спочатку — маленький експеримент:
    - Запустити 2 cmux панелі на одному акаунті
    - В CCSwitch UI swap token manually
    - Перевірити чи 2-й термінал бачить swap на наступному запиті
   (чи залишається на старому)
    - Це 10-хвилинний тест
  2. Якщо CLI перечитує Keychain → approve архітектуру, пишу
  новий spec (~80% коротший поточного)
  3. Якщо CLI кешує агресивно → думаємо mitigation (tmux nudge з
  /logout + /login? Restart panes?)

  Хочеш почати з empirical тесту?

✻ Cooked for 1m 10s · 4 shells still running

❯ як це зазвичай відбувається. Я працюю в терміналах, на всіх
  одночасно досягаю лімітів. На одному з них роблю новий /login.
   На решту треба лише надіслати одне повідомлення, щоб
  розбудити його.
  ⎿  UserPromptSubmit hook error                                    ⎿  Failed with non-blocking status code: Error: LETTA_API_KEY
                                                                       environment variable is not set



⏺ ВІДМІННО. Ти щойно підтвердив empirically найголовніше: Claude
  Code CLI перечитує Keychain після swap (інакше твій workflow не
   працював би). Один nudge-message на пане = CLI бачить нові
  credentials. Це саме те що нам треба.

  Фінальна архітектура — підтверджено

  Твій manual workflow:
  20 панелей хітають limit
  ↓
  /login в одній панелі → новий акаунт в Keychain
  ↓
  В решті 19 панелей одне повідомлення → прокидаються з новими
  credentials

  CCSwitch просто автоматизує це:
  Probe detect hit threshold
  ↓
  Атомарний swap Keychain: CCSwitch-vault-accountB → Claude
  Code-credentials
  ↓
  Update ~/.claude/.claude.json (oauthAccount, userID)
  ↓
  tmux nudge всі claude панелі (вже implemented у CCSwitch!)
  ↓
  20 CLI прокидаються на новому акаунті

  Наслідки для кодабази

  Видалити (~1500 LOC):
  - Всю CLAUDE_CONFIG_DIR інфраструктуру
  - backend/services/account_service.py::activate_account_config
  (4-артефактну послідовність) — зводиться до одного Keychain
  write
  - Весь credential_targets модуль (mirror to multiple files —
  непотрібно, тепер один ~/.claude/.claude.json)
  - force_refresh_config_dir + всі refresh call-sites в CCSwitch
  - Active-ownership model з background.py
  (_REFRESH_SKEW_MS_INACTIVE, _waiting set, etc.)
  - stale_reason для transient станів (залишити тільки terminal
  revocation)
  - Папки ~/.ccswitch-accounts/account-<hash>/*
  - Папки ~/.claude-accounts/* + симлінк ~/.claude
  - Per-config-dir hashed Keychain entries (cleanup)
  - _force_refresh_locks dict
  - DB column accounts.config_dir

  Додати (~200 LOC):
  - Vault service: ccswitch-vault-<email> Keychain entries per
  account
  - Swap orchestrator: atomic Keychain write + .claude.json
  update + nudge
  - Migration: видалити старі structures, перенести user state в
  ~/.claude/

  Зберегти без змін:
  - tmux nudge (вже рахує, робить точно що треба)
  - Usage probe (тепер просто читає standard Keychain entry)
  - WebSocket broadcast (події ті самі)
  - Auto-switch decision logic

  Net change: −1300 LOC.

  Race analysis (фінальна)

  - 20 CLI → один Keychain entry → upstream 2.1.101 fix handles
  coordination
  - CCSwitch не refresh'ить нічого → немає CLI↔CCSwitch race під
  час normal op
  - Swap трапляється на rate-limit moment → всі CLI отримують 429
   (не 401) → ніхто не мid-refresh'ить → swap window безпечний
  - Manual switch в healthy state: тонкий race window,
  recoverable через detect+retry

  Залишилось зробити

  1. Написати новий spec (короткий, ~30% об'єму попереднього) —
  фіксуватиме нову архітектуру
  2. Пометити старий spec
  (2026-04-14-active-ownership-refresh-fix-design.md і
  2026-04-15-multi-account-race-free-architecture-design.md) як
  SUPERSEDED
  3. Migration plan:
    - Merge ~/.claude-accounts/nazariileus/* → ~/.claude/ (break
  симлінк)
    - Видалити ~/.claude-accounts/leusnazarii*/ (legacy)
    - Видалити ~/.ccswitch-accounts/ (всі 15)
    - Cleanup orphan Keychain entries
    - DB migration: drop config_dir, add vault_service_name
    - Для 5 burned акаунтів: re-login через CCSwitch UI після
  deploy (отримаємо fresh refresh_tokens у vault)

  Приступаю до написання spec? Після apprval — implementation +
  migration.

✻ Cogitated for 1m 17s · 4 shells still running

❯ ми все ще будемо слідкувати за реальним лімітом аккаунтів?
  будемо бачити в реальному часі ліміти по кожному, зможемо
  свапати коли треба щоб не досягати лімітів?
  ⎿  UserPromptSubmit hook error                                    ⎿  Failed with non-blocking status code: Error: LETTA_API_KEY
                                                                       environment variable is not set



⏺ Так, і тепер ця частина стає чистішою ніж зараз.

  Як моніторинг працює в новій архітектурі

  Key insight: в новій моделі inactive акаунти живуть у
  приватному CCSwitch vault'і (Keychain entries
  ccswitch-vault-<email>). CLI їх не бачить взагалі — CLI читає
  тільки standard Claude Code-credentials. Це автоматично робить
  CCSwitch єдиним власником refresh_token'ів для inactive
  акаунтів.

  Active акаунт (той що зараз в standard entry)

  - CLI refresh'ить (20 панелей координуються 2.1.101 fix'ом)
  - CCSwitch читає access_token з standard entry → probe
  /v1/messages → real-time usage
  - Probe кожні 15s під час WS connection active (як зараз)

  Inactive акаунти (в CCSwitch vault)

  - CCSwitch тепер безпечно refresh'ить їх — бо ніякий CLI не має
   цих refresh_token'ів в пам'яті, немає кого гонити
  - CCSwitch читає vault entry → якщо access_token протух →
  викликає /oauth/token сам → оновлює vault + отримує fresh
  access_token
  - Probe /v1/messages з цим fresh token'ом → real-time usage для
   inactive акаунта
  - Refresh race неможлива за дизайном (єдиний власник vault
  entry)

  Чому це краще ніж поточна архітектура

  Поточна (broken): inactive акаунти живуть у Claude
  Code-credentials-<hash> (CLAUDE_CONFIG_DIR-hashed). CLI їх
  бачить якщо у cmux експортнуто відповідний CLAUDE_CONFIG_DIR.
  CCSwitch refresh'ить → CLI теж refresh'ить → race → burn.

  Нова: inactive акаунти в ccswitch-vault-* (різний Keychain
  service name). CLI їх не бачить технічно — не існує механізму в
   CLI щоб читати ці entries. CCSwitch єдиний refresher. Гонка
  неможлива by design.

  Auto-switch logic

  Точно такий самий як зараз:
  Для кожного акаунта (active + всі inactive):
    якщо rate-limit headers показують > threshold_pct →
  auto-switch кандидат

  get_next_account: сортує за priority серед eligible (not
  rate-limited, not stale)
  perform_switch: vault → standard + nudge

  Ти бачиш в UI в real-time:
  - Поточний активний акаунт з його usage bars (оновлюється кожні
   15s через CLI-refreshed tokens)
  - Всі inactive акаунти з їх usage bars (оновлюється кожні 15s
  через CCSwitch-refreshed vault tokens)
  - Коли активний hit threshold → auto-switch на найкращого
  inactive → nudge

  Тонкий момент на swap

  На момент самого swap'а є коротке вікно де:
  - Токени акаунта A (вихідного) в vault MAY бути трохи застарілі
   (CLI міг rotate'нути refresh_token і CCSwitch ще не прочитав)
  - Токени акаунта B (вхідного) з vault йдуть у standard entry →
  CLI тепер бачить акаунт B

  Mitigation при swap:
  1. Перед перенесенням A→vault: CCSwitch читає standard Keychain
   entry останній раз, сохраняє LATEST state A в vault (так що
  CLI's найсвіжіший refresh не губиться)
  2. Потім B→standard
  3. Update .claude.json
  4. Nudge

  Виходить atomic з точки зору vault consistency.

  Резюме

  ✅ Real-time моніторинг ВСІХ N акаунтів збережений
  ✅ Auto-switch перед лімітом — той самий логіка
  ✅ Refresh race неможлива (Keychain partition by service name,
  CCSwitch і CLI ніколи не бачать один і ті самі entries
  одночасно)
  ✅ User workflow (bare claude, ~/.claude/, один активний) —
  standard Claude flow

  Переходжу писати новий spec?
========================================================================================================================
PRE HISTORY END
===========
WHAT-TO-DO:

You are the Team Lead agent in Agent Teams mode. Your job is to fully redesign and implement the new CCSwitch multi-account architecture across backend, migration layer, storage, and UI integration.

This is not a small refactor. This is a controlled architectural rewrite. You must lead with sub-agents, supervise them closely, verify each stage, and integrate everything yourself. Treat this as a production-grade migration with zero tolerance for half-finished work, stale abstractions, compatibility shims, or “temporary” code.

You are not preserving backward compatibility. This is a hard cutover.
Do not keep legacy infrastructure alive.
Do not build compatibility wrappers.
Do not leave dual paths.
Do not preserve old data structures unless they are strictly required for one-time migration and then removed.
Prefer deletion over adaptation.

The empirical result below is already confirmed and is the source of truth:

CONFIRMED ARCHITECTURE FACT
Claude Code CLI re-reads Keychain credentials after credential swap. One nudge message in each tmux pane is enough for sleeping CLI processes to wake up and observe the new credentials. This has been verified empirically.

FINAL TARGET ARCHITECTURE
Manual workflow already proved:
20 panes hit limit
→ login in one pane writes new account credentials to Keychain
→ send one message to other 19 panes
→ all CLI sessions continue using the new credentials

CCSwitch must automate exactly this model:
1. Probe detects threshold / switch condition
2. Atomic Keychain swap:
   CCSwitch vault account entry → standard Claude Code credentials entry
3. Update ~/.claude/.claude.json with the active oauthAccount and userID
4. tmux nudge all Claude panes
5. All active CLI sessions wake up on the new account

This replaces the old multi-config-dir / mirrored-credentials architecture.

NON-NEGOTIABLE GOALS
- Implement the new architecture cleanly
- Remove obsolete architecture completely
- Keep code precise, minimal, understandable, and production-ready
- Verify backend, migration, storage, and UI behavior end-to-end
- Ensure all account switching behavior is consistent with the new model
- Ensure the resulting system is simpler than the current one
- No backward compatibility
- No legacy ownership/refresh model
- No partially dead code left behind
- No speculative abstractions
- No TODO placeholders, mocks, scaffolding, or “future extension” clutter

WHAT MUST BE REMOVED
Delete or fully replace the old architecture around:
- all CLAUDE_CONFIG_DIR infrastructure
- backend/services/account_service.py::activate_account_config
  and its old 4-artifact activation sequence
- credential_targets module and all “mirror credentials to multiple files” logic
- force_refresh_config_dir and all refresh call-sites in CCSwitch
- active-ownership model in background.py
  including _REFRESH_SKEW_MS_INACTIVE, waiting sets, inactive-refresh behavior, etc.
- stale_reason states used for transient refresh ownership conditions
  keep only genuinely terminal revocation/failure semantics if still needed
- ~/.ccswitch-accounts/account-<hash>/* structures
- ~/.claude-accounts/* structures and ~/.claude symlink architecture
- per-config-dir hashed Keychain entries
- _force_refresh_locks dict and related logic
- DB column accounts.config_dir
- any tests that validate obsolete architecture
- any docs/specs that describe obsolete architecture as current

WHAT MUST BE ADDED
Implement the new architecture with roughly these core pieces:
- Vault service:
  one Keychain vault entry per account, named like ccswitch-vault-<email> or equivalent stable convention
- Swap orchestrator:
  atomic write from vault credential → standard Claude Code credential target,
  then update ~/.claude/.claude.json,
  then nudge tmux panes
- Migration path:
  move surviving user state into ~/.claude/,
  remove old account/config-dir structures,
  clean legacy artifacts and orphan credential entries
- Clean active account model:
  account activation means “this account is now copied into the standard Claude Code credential location and reflected in ~/.claude/.claude.json”
- DB migration:
  drop config_dir,
  add vault_service_name or equivalent field only if truly needed by the final architecture
- UI/backend updates wherever old assumptions depended on config_dir or legacy ownership model

WHAT SHOULD STAY CONCEPTUALLY INTACT
Keep and adapt only where needed:
- tmux nudge mechanism
- usage probe
- WebSocket broadcast/event model
- auto-switch decision logic

But make sure they now operate against the new single-credential architecture.

RACE MODEL TO IMPLEMENT AGAINST
Treat the following as the intended design model:
- 20 CLI sessions share one standard Keychain entry
- upstream coordination in Claude Code handles same-entry behavior
- CCSwitch does not do legacy refresh ownership coordination anymore
- normal operation must not recreate CLI↔CCSwitch refresh races
- swap usually happens near rate-limit moment, where 429 behavior makes swap window acceptable
- manual switch during healthy state may still have a narrow race window; handle it with clear detection/retry semantics, not with old ownership architecture

YOUR EXECUTION MODEL
You are the lead agent. Use sub-agents aggressively and deliberately.
You must decompose the work, assign specialized agents, review their output, and integrate only after verification.

Create at least these workstreams, whether as explicit sub-agents or equivalent isolated flows:
1. Architecture/spec agent
   - audits current codebase
   - maps old architecture to files/modules/tests/docs
   - writes the new concise spec
   - marks old specs as superseded
2. Backend/core refactor agent
   - implements vault service
   - implements swap orchestrator
   - rewrites account activation flow
   - removes legacy architecture
3. Migration/storage agent
   - designs and implements filesystem cleanup and migration
   - designs DB migration
   - handles Keychain cleanup and vault naming
4. UI/integration agent
   - checks API/UI assumptions
   - fixes frontend/backend integration impacted by config_dir removal
   - ensures switch state, active account state, and status displays still work correctly
5. QA/reviewer agent
   - independently reviews architecture, failure modes, race handling, migration safety, and code quality
   - validates that backward compatibility was not accidentally preserved

Sub-agents may spawn narrower sub-agents if useful.
You, the lead, are responsible for the final integrated result.
Do not delegate away final judgment.

WORKING STYLE
- Start by auditing the current codebase and mapping every impacted file/module/test/doc
- Then write the replacement spec before major implementation
- Then execute in controlled phases
- After each major phase, run verification before proceeding
- Keep a running task ledger and update it as you learn more
- Do not ask for constant approvals
- Only surface blocking ambiguities when absolutely necessary
- When something old conflicts with the new architecture, the new architecture wins
- When in doubt, simplify

DELIVERABLES
You must produce all of the following, not just code changes:
1. New architecture spec
   - concise, much shorter than prior specs
   - clearly describes the new single-credential + vault + nudge architecture
2. Old spec supersession
   Mark these as SUPERSEDED and explain why:
   - 2026-04-14-active-ownership-refresh-fix-design.md
   - 2026-04-15-multi-account-race-free-architecture-design.md
3. Full implementation
4. Migration implementation and cleanup logic
5. DB migration(s)
6. Updated or replaced tests
7. Updated docs where needed
8. Final verification report with:
   - what was removed
   - what was added
   - remaining known risks, if any
   - proof that backward compatibility was intentionally removed
   - proof that UI/backend/account switching still works coherently

PHASES YOU MUST FOLLOW

PHASE 1 — AUDIT AND PLAN
- Inspect the repository thoroughly
- Identify all code paths tied to:
  - config_dir architecture
  - credential mirroring
  - refresh ownership / active ownership
  - stale transient states
  - account activation workflow
  - filesystem account layouts
  - DB schema assumptions
  - UI/backend assumptions
- Build a concrete impact map
- Build a deletion map
- Build an implementation plan ordered by dependency

PHASE 2 — SPEC FIRST
Write the new spec before major code changes.
The spec must:
- state the empirical finding clearly
- define the final architecture unambiguously
- define activation/swap semantics
- define vault semantics
- define migration semantics
- define race expectations
- explicitly state that backward compatibility is not preserved
Then mark the two old specs as SUPERSEDED.

PHASE 3 — CORE REWRITE
Implement the new architecture in backend/core logic:
- vault service
- standard credential swap
- ~/.claude/.claude.json update
- account activation semantics
- removal of config-dir-based activation
- removal of refresh ownership architecture
- removal of credential mirroring architecture

PHASE 4 — MIGRATION
Implement migration and cleanup:
- merge surviving user state into ~/.claude/
- break/remove legacy symlink architecture
- remove ~/.claude-accounts legacy structures
- remove ~/.ccswitch-accounts structures
- clean orphan Keychain entries
- update DB schema
- ensure migrations are idempotent where practical, but do not preserve old runtime architecture

PHASE 5 — UI / API / STATE INTEGRATION
Check all UI/backend surfaces affected by the rewrite:
- active account state
- switch state
- error state
- account metadata
- websocket events
- any assumptions that referred to config_dir or per-dir ownership
Fix them properly.

PHASE 6 — TESTING AND VERIFICATION
Add or update tests for:
- vault lookup / vault naming
- swap orchestration
- ~/.claude/.claude.json updates
- removal of old activation behavior
- migration behavior
- failure/retry semantics around swap
- UI/backend integration points that changed
Also remove obsolete tests.

PHASE 7 — FINAL REVIEW PASS
Do a ruthless final cleanup pass:
- delete dead code
- remove no-longer-used types, helpers, comments, docs, migrations, constants, and tests
- simplify names
- verify no backward compatibility layer remains
- verify no stale architecture language remains in docs/comments
- verify final code is the minimal correct shape for the new architecture

IMPLEMENTATION STANDARDS
- Clean, direct code
- No overengineering
- No giant god-classes unless unavoidable
- No hidden side effects
- No magic constants without names
- Strong typing where the codebase expects it
- Clear invariants
- Explicit failure handling
- Clear logs only where operationally useful
- Prefer small, composable functions over sprawling conditionals
- Every deletion should be intentional
- Every surviving abstraction must justify its existence

VERIFICATION GATES
Before moving from one major phase to the next, verify:
- architecture still matches the target model
- no unexpected dependency on config_dir remains
- no legacy credential fan-out remains
- no active-ownership refresh logic remains
- no UI/backend mismatch remains
- tests for the changed layer pass
- docs stay aligned with code

SEARCH TARGETS / SMELLS
Treat these patterns as suspicious and remove or justify them:
- config_dir
- claude-accounts
- ccswitch-accounts
- credential_targets
- activate_account_config
- force_refresh
- active ownership
- refresh skew
- inactive refresh
- waiting ownership
- mirrored credentials
- per-config keychain targets
- symlink-based account activation
- stale transient ownership states

MIGRATION NOTES TO COVER
The target migration includes at least:
- merge ~/.claude-accounts/nazariileus/* into ~/.claude/ if still relevant in repo behavior
- remove ~/.claude-accounts/leusnazarii* legacy structures if still relevant
- remove ~/.ccswitch-accounts/* legacy account structures
- clean orphan Keychain entries from old hashed/per-config layouts
- DB migration dropping config_dir and adding vault reference field only if necessary
- burned accounts can be re-logged via CCSwitch UI after deploy; do not build special compatibility machinery for them

IMPORTANT DECISION RULES
- Do not preserve a legacy mechanism “just in case”
- Do not create toggles between old/new architecture
- Do not defer cleanup to later
- Do not leave migration half-complete
- Do not stop at “tests pass” if architecture is still messy
- Do not trust old docs over empirical validation and current code inspection
- When two possible designs exist, choose the simpler one that matches the confirmed architecture

EXPECTED OUTPUT FORMAT WHILE WORKING
As you work, keep reporting concise progress in this structure:
- Current phase
- What was audited/changed
- What was verified
- Risks or surprises
- Next concrete step

At the end, provide:
1. summary of architectural changes
2. list of deleted legacy components
3. list of added/reworked components
4. migration summary
5. test/verification summary
6. remaining risks, if any
7. explicit statement that backward compatibility was intentionally not preserved

Now begin immediately:
- audit the repository
- map all impacted files and modules
- write the new spec
- mark the old specs as superseded
- then implement the rewrite and migration
- verify relentlessly after every major step
- finish only when the codebase reflects the new architecture cleanly end-to-end
