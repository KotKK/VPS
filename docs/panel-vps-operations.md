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
install -m 0644 gateway/deploy/systemd/gateway-health.service /etc/systemd/system/gateway-health.service
install -m 0644 gateway/deploy/systemd/gateway-health.timer /etc/systemd/system/gateway-health.timer
install -d -m 0700 /var/lib/awg-gateway /run/awg-gateway
touch /var/lib/awg-gateway/known_hosts
chmod 0600 /var/lib/awg-gateway/known_hosts
systemctl daemon-reload
systemctl enable --now gateway-health.timer
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

## Обновления ядра и AmneziaWG

На шлюзе и выходных VPS должен быть установлен метапакет заголовков ядра:
`linux-headers-amd64` для обычного ядра Debian или `linux-headers-cloud-amd64`
для cloud-ядра. Установка только `linux-headers-$(uname -r)` не обеспечивает
сборку модуля при последующих автоматических обновлениях Linux.

Если после перезагрузки службы AWG завершаются с `Unknown device type`, проверьте
`uname -r`, `dkms status` и `modinfo amneziawg`. Если для текущего ядра нет модуля,
установите соответствующие заголовки вместе с метапакетом и дождитесь успешной
сборки DKMS. Затем загрузите `modprobe amneziawg` и запустите службы туннелей.
Профили клиентов и ключи при этом менять не требуется.

Сами клиентские peer-записи восстанавливаются из `clients.sqlite3` при каждом
запуске `awg-clients.service`: drop-in `restore-peers.conf` запускает
`python -m gateway.app.client_peers` после создания интерфейса и загрузки
серверного ключа. Пустые незавершённые регистрации игнорируются, существующие
профили не пересоздаются. Загрузка выполняется до запуска панели и ботов.
Проверяйте не только наличие интерфейса, но и количество peer-записей:
`awg show awg-clients peers | wc -l`.

## Контроль российского шлюза

`gateway-health.timer` раз в минуту проверяет также сам шлюз: модуль AmneziaWG,
пакет заголовков для будущих обновлений, модули для новых ядер в `/boot`, пересылку
IPv4, состояние клиентского интерфейса и соответствие загруженных peer-записей
базе клиентов. Выключенное устройство клиента не считается аварией: свежий
handshake для этой проверки не требуется.

После двух последовательных неудачных проверок отправляется отдельное сообщение
«Российский VPS» в MAX и Telegram, а после устранения проблемы — сообщение о
восстановлении. Устойчивый отказ не вызывает повторных сообщений каждую минуту.
MAX использует прямой российский канал. Telegram продолжает использовать VPN;
если все выходы недоступны, его сообщение сохраняется в отдельной очереди до
восстановления связи. При полном отключении самого VPS уведомление с него не
может отправиться — для такого случая нужен мониторинг с отдельного сервера.

Мониторинг не перезагружает сервер и не пересоздаёт клиентские ключи. Если модуль
для следующего ядра ещё не подготовлен, приходит предупреждение до перезагрузки.

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
