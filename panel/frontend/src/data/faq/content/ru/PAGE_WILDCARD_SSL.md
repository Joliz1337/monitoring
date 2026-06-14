# Wildcard SSL

Выпуск, обновление и раскатка wildcard-сертификата (`*.example.com`) на несколько серверов. Работает через Let's Encrypt + Cloudflare DNS challenge.

## Что делает
- Выпускает и обновляет wildcard-сертификат (вручную или автоматически перед истечением)
- Разворачивает его на серверы с указанием пути и команды reload
- Показывает дату истечения и days-left по каждому сертификату

## Параметры
- **Deploy Path** — куда класть файлы на сервере, обычно `/etc/letsencrypt/live/<domain>/`. Там `fullchain.pem` и `privkey.pem`.
- **Reload Cmd** — команда перезапуска веб-сервера, обычно `systemctl reload nginx` или `systemctl reload haproxy`.
- **Renew Days Before** — за сколько дней до истечения автообновлять. По умолчанию 30.
- **Cloudflare API Token** — права `Zone.Zone.Read` и `Zone.DNS.Edit` на нужную зону.

## Полезно знать
- Один wildcard покрывает все поддомены, включая новые — не нужно каждый раз гонять ACME.
- Для wildcard работает только DNS challenge (HTTP не поддерживается), отсюда нужен Cloudflare.
- Без Reload Cmd сервер продолжит отдавать старый сертификат, пока не перезапустится.
- `days left` отрицательный — сертификат истёк; чаще всего причина в истёкшем Cloudflare Token.
