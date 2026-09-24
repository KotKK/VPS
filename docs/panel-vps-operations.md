# Управление зарубежными VPS через панель

Панель доступна только через SSH-туннель:

```bash
ssh -L 8080:127.0.0.1:8080 root@<IP_РОССИЙСКОГО_VPS>
```

После подключения откройте `http://127.0.0.1:8080/exits`.

## Резервная копия перед обновлением

```bash
install -d -m 0700 /root/awg-gateway-backup
cp -a /var/lib/awg-gateway /root/awg-gateway-backup/state
cp -a /etc/amnezia /root/awg-gateway-backup/amnezia
```

## Обновление российского шлюза

```bash
cd /opt/awg-gateway
git pull --ff-only
.venv/bin/pip install -e .
install -d -m 0755 /usr/local/libexec
install -m 0700 gateway/deploy/remote-exit.sh /usr/local/libexec/awg-gateway-remote-exit
install -m 0644 gateway/deploy/systemd/gateway-web.service /etc/systemd/system/gateway-web.service
install -d -m 0700 /var/lib/awg-gateway /run/awg-gateway
touch /var/lib/awg-gateway/known_hosts
chmod 0600 /var/lib/awg-gateway/known_hosts
systemctl daemon-reload
systemctl restart gateway-web.service
systemctl --no-pager --full status gateway-web.service
```

## Добавление VPS

Поддерживаются чистые Debian 12 и Debian 13. В панели укажите название, IPv4-адрес и `<ПАРОЛЬ_НОВОГО_VPS>`. Логин всегда `root`. Пароль не сохраняется: он находится только в памяти фоновой задачи до её завершения.

Нормальная последовательность этапов: подключение по SSH, проверка Debian 12/13, установка пакетов, настройка зарубежного туннеля, настройка российского шлюза, handshake, проверка интернета, подключение к балансировке.

На Debian 12 установщик собирает совместимый модуль ядра и утилиты из официальных репозиториев AmneziaWG с зафиксированными commit-хешами. На VPS должны быть доступны заголовки текущего ядра; контейнер LXC должен разрешать загрузку модуля `amneziawg` на хосте.

- `Установка` — операция выполняется; её можно остановить между этапами.
- `Доступен` — handshake и интернет проверены, VPS участвует в новых соединениях.
- `Ошибка` — показана безопасная причина; введите актуальный пароль и нажмите «Повторить».
- `Удаляется` — VPS уже исключается из балансировки и очищается.

## Удаление

Для удалённой очистки введите текущий пароль `root`. Сначала VPS исключается из балансировки, затем удаляется локальный туннель. Если зарубежный сервер недоступен, панель сохранит предупреждение; его можно очистить повторным удалением. Удаление последнего доступного выхода требует отдельного флажка подтверждения.

## Проверка

```bash
awg show
ip rule list
nft list table inet awg_gateway
sqlite3 /var/lib/awg-gateway/exits.sqlite3 'select name,address,status,stage,slot from exits order by slot;'
```

В таблице `exits` отсутствует поле пароля. После перезапуска `gateway-web.service` список VPS и их стабильные сетевые слоты должны сохраниться.

## Откат

Если обновление панели не запускается, вернитесь к предыдущему коммиту без удаления состояния:

```bash
cd /opt/awg-gateway
git log --oneline -5
git checkout <ПРЕДЫДУЩИЙ_COMMIT>
.venv/bin/pip install -e .
systemctl restart gateway-web.service
```

Не удаляйте `/var/lib/awg-gateway` и `/etc/amnezia`: там находятся состояние панели и рабочие ключи туннелей.
