# ACME + Cloudflare API

Автоматический выпуск wildcard-сертификатов через Let's Encrypt с DNS challenge на Cloudflare.

## Как это работает
1. Вводите домен и жмёте Issue — панель просит у Let's Encrypt сертификат на `*.example.com`.
2. Let's Encrypt требует подтвердить владение: TXT-запись `_acme-challenge.example.com`.
3. Панель через Cloudflare API создаёт эту запись, Let's Encrypt проверяет и выдаёт сертификат.
4. Панель удаляет временную TXT-запись, сертификат сохраняется в БД панели.

## Что нужно
- **Cloudflare API Token** с правами `Zone.Zone.Read` и `Zone.DNS.Edit` на зону. Создаётся на https://dash.cloudflare.com/profile/api-tokens.
- **Email** — для регистрации ACME-аккаунта и уведомлений об истечении, не публикуется.
- **Renew Days Before** — за сколько дней до истечения обновлять. По умолчанию 30.

## Полезно знать
- Для wildcard работает только DNS challenge: он доказывает владение всей зоной, а не одним сервером.
- DNS не в Cloudflare — перенесите зону или загрузите готовый сертификат как «свой».
- Лимиты Let's Encrypt: 20 сертификатов на домен в неделю, 5 дубликатов, 300 заказов за 3 часа. Для тестов — staging (без лимитов, но браузеры не доверяют).
- `fullchain.pem` и `privkey.pem` хранятся в БД панели и копируются на ноды по SSH; приватный ключ не покидает панель и агенты.
- Частые ошибки: `invalid token` (токен истёк), `zone not found` (нет доступа к зоне), `timeout waiting for DNS` (повторите).
