# Freelance orders watcher

Собирает заказы с Kwork, FL.ru и Freelance.ru в SQLite (`market.db`) и присылает в Telegram
новые заказы по нишам: ИИ-внедрение, данные, 3D, игры, мобильные приложения, автоматизация маркетплейсов.

- `collect.py` — сборщик (Kwork: полные поля, бюджет, отклики; `--full` — вся лента).
- `notify.py` — фильтр по нишам и отправка в Telegram (`--setup <токен>`, `--test N`).
- `.github/workflows/notify.yml` — запуск каждые 10 минут в GitHub Actions.
  Нужны секреты `TG_TOKEN` и `TG_CHAT_ID`.

Источники FL.ru и Freelance.ru в `sources/` взяты из
[IsWake77/FreelanceParser](https://github.com/IsWake77/FreelanceParser).
