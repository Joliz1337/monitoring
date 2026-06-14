# Sticky sessions

Привязка клиента к одному backend, чтобы его запросы всегда шли на тот же сервер. Нужна, когда приложение хранит сессию в памяти процесса (не в БД/Redis).

## Параметры
- **Cookie-based** — HAProxy вставляет в ответ cookie (`SERVERID=srv2`) и читает её в следующих запросах. Только HTTP, ловит «своих» клиентов надёжно.
- **Stick-table (по IP)** — таблица «IP клиента → backend». Работает для любого трафика (HTTP, TCP), но клиенты за NAT уйдут на один сервер.

## Как настроить
Cookie-based:
1. В backend: `cookie SERVERID insert indirect nocache`
2. Каждому серверу имя cookie: `server srv1 10.0.0.1:80 check cookie srv1`
3. HAProxy вставит `Set-Cookie: SERVERID=srv1`, следующие запросы пойдут на srv1

Stick-table:
1. `stick-table type ip size 200k expire 30m`
2. `stick on src`
3. Первый запрос от IP пишет запись, следующие 30 минут идут на тот же backend

## Полезно знать
- Не нужна для stateless API и когда сессия в БД/Redis/Memcached.
- Сервер, к которому «прилип» клиент, упал — HAProxy перекинет на другой, но сессия пропала: клиент разлогинится.
- Sticky слегка ломает балансировку: долгая сессия грузит один сервер непропорционально.
- Не действует — проверьте, что настроены алгоритм + cookie/stick-table и сделан reload.
