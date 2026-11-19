# partizan backend

Выдаёт приложению подходящий код SSO из выделенного Gmail. Telegram-уведомления — необязательная опция.

## Развёртывание

В Portainer создайте stack из этого Git-репозитория, ветка `main`, Compose path `compose.portainer.yaml`.
В переменных stack заполните:

```dotenv
MAIL_HOST=imap.gmail.com
MAIL_USERNAME=выделенный_ящик@gmail.com
MAIL_PASSWORD=пароль_приложения_Google
SSO_MAIL_SENDER=точный_адрес_отправителя_кодов
```

Нужны существующие Docker-сети `proxy` и `vpn`. В Nginx Proxy Manager направьте HTTPS-поддомен на `http://partizan-backend:8787`.
Проверка: `/health` → `{"ok":true}`. В консоли контейнера `python backend.py mail-check` проверяет доступ к Gmail без вывода писем и кодов.

GitHub Actions проверяет код и публикует готовые образы amd64/arm64. Для обновления Git-stack добавьте webhook Portainer в repository secret `PORTAINER_WEBHOOK_URL`.
Имя образа `ghcr.io/narvik6/partizan-telegram-bot:latest` и постоянный том сохранены для совместимости.

Локально: скопируйте `.env.example` в `.env`, заполните значения, выполните `docker compose up -d --build`.
Без Docker: `pip install -r requirements.txt`, затем `python backend.py`.

## Использование

Настройте пересылку кодов на выделенный Gmail. В partizan откройте «Настройки → Вход и автоматизация», укажите учебную почту, пароль SSO. Один раз войдите вручную с кодом: доступ к получению следующих кодов сохранится автоматически.
[Настройка Gmail](docs/mail.md).

Для Telegram задайте `BOT_TOKEN` в stack. Без него эта опция выключена.
Отправьте боту `/start`, получите фразу из двух русских существительных и укажите её в настройках приложения.
Для группы: администратор сначала открывает личный чат с ботом, затем отправляет `/connect@ИмяБота` в группе; фраза придёт ему в личный чат.
`/stop` отключает чат. Для замены старого длинного кода короткой фразой отправьте `/connect`.

[API и техническая документация](docs/api.md).
