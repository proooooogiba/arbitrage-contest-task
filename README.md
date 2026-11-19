# Spot Arbitrage Contest

Напишите Python-стратегию taker–taker арбитража между двумя spot-биржами.
Покупайте актив на одной площадке и продавайте на другой с учётом комиссий,
задержек и доступной глубины.

В реальном тесте вход парный, остаток после исполнения нужно захеджировать
за 2 секунды. [Торговые правила](docs/strategy-contract.md).

Условия участия и требования к добросовестности изложены в
[политике участия](docs/fair-play.md).

В репозитории находятся программа локальной оценки (`spot_contest`), примеры
стратегий и инструкции.

Parquet-файлы, manifest и конфиг находятся в разделе **Files** на CodaBench.
[Файлы соревнования](https://www.codabench.org/competitions/18505/),
[правила теста](docs/real-data-test.md).

## Задание и оценка

Стратегия получает доступные события рынка и отчёты об исполнении, отправляет
заявки `Limit IOC`. Score учитывает комиссии и восстановление стартовых запасов
на каждой бирже. Чем выше score, тем лучше. [Расчёт результата](docs/architecture.md#итоговый-результат).

## Локальный запуск

Нужны Git и Python 3.9+, рекомендуется Python 3.11.
Команды рассчитаны на Linux/macOS. В Windows используйте WSL2.
Для Docker-запуска нужен Docker Engine / Docker Desktop.

```bash
git clone https://github.com/proooooogiba/arbitrage-contest-task.git
cd arbitrage-contest-task
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -c requirements.lock -e .

# Создать синтетический пример
.venv/bin/python -m spot_contest demo --output data/demo
cp -R examples/kalman_pairs my_strategy

# Запустить свою стратегию без Docker
.venv/bin/python -m spot_contest evaluate \
  --config data/demo/config.json --data data/demo/events.jsonl \
  --submission my_strategy --output runs/debug-001 --runner subprocess
```

`subprocess` запускает код без файловой и сетевой изоляции. Используйте его для
своей стратегии. Перед отправкой проверьте её в Docker, как при серверной оценке.

```bash
docker build -f Dockerfile.strategy -t spot-contest-strategy:0.1.0 .
.venv/bin/python -m spot_contest evaluate \
  --config data/demo/config.json --data data/demo/events.jsonl \
  --submission my_strategy --output runs/docker-001
```

Для каждого запуска выбирайте новый каталог `--output`.
Команду `demo` достаточно выполнить один раз.
Короткий demo проверяет загрузку стратегии и API. Торговое поведение Kalman
проверяйте на часовом quickstart из Train.

| Файл результата | Содержание |
|---|---|
| `runs/<запуск>/scores.json` | Score, PnL, комиссии и другие метрики |
| `runs/<запуск>/result.json` | Балансы и терминальное восстановление |
| `runs/<запуск>/audit.jsonl` | События, заявки и исполнения |
| `runs/<запуск>/run.json` | Статус запуска и причина ошибки при `failed` |

## Данные с CodaBench

Скачайте архив Train из раздела **Files** и распакуйте в `data/train/`.
[CodaBench](https://www.codabench.org/competitions/18505/).

В архиве есть Parquet-файлы, manifest `sources.json`, конфиг `config.json` и
готовый часовой пример `quickstart/events.jsonl`.

Для локальной оценки используйте актуальный конфиг реального теста.
[Конфиг](examples/real-test-config.json).

Часовой пример можно запустить сразу.

```bash
.venv/bin/python -m spot_contest evaluate \
  --config examples/real-test-config.json --data data/train/quickstart/events.jsonl \
  --submission my_strategy --output runs/quickstart-001
```

Для полных Parquet-файлов сначала выполните `prepare`.
Команда преобразует их в `events.jsonl` для оценки.

```bash
.venv/bin/python -m pip install -c requirements.lock -e '.[parquet]'
.venv/bin/python -m spot_contest prepare \
  --manifest data/train/sources.json --output data/train/events.jsonl
.venv/bin/python -m spot_contest validate \
  --config examples/real-test-config.json --data data/train/events.jsonl
.venv/bin/python -m spot_contest evaluate \
  --config examples/real-test-config.json --data data/train/events.jsonl \
  --submission my_strategy --output runs/train-001
```

На сервере действуют правила соревнования. Формат исходных файлов описан отдельно.
[Контракт данных](docs/data-contract.md).

## Стратегия и отправка

Файл `my_strategy/strategy.py` должен определять класс `Strategy` с методами
`on_start(config, portfolio)`, `on_market_event(event, portfolio)` и
`on_execution_report(report, portfolio)`.

Каждый метод возвращает список заявок или `None`. Цены и количества передаются
строками, время — в наносекундах. [Полный API](docs/strategy-contract.md).

- [Kalman](examples/kalman_pairs/README.md) — основной пример с прогнозом спреда,
  обработкой частичных исполнений и хеджированием остатка. Готовая модель входит
  в каталог, пример можно запускать сразу.
- [baseline](examples/baseline/strategy.py) — простой арбитраж с фильтром комиссий.
  После отчётов обеих ног закрывает остаток; при отсутствии свежей ликвидности
  повторяет попытку на следующем снимке стакана.

Среда стратегии — Python 3.11 и стандартная библиотека. Доступа к сети и установки
пакетов во время оценки нет. Дополнительные библиотеки доступны, только если
организатор заранее включил их в среду оценки.

Для скопированного примера Kalman поместите четыре файла в корень ZIP.

```bash
cd my_strategy
../.venv/bin/python -m zipfile -c ../submission.zip \
  strategy.py execution.py parameters.json model.json
cd ..
.venv/bin/python -m zipfile -l submission.zip
```

Для baseline достаточно `strategy.py`. Собственные модули и веса добавляйте
с сохранением относительных путей.

Исключите из архива evaluator, данные, `.venv`, конфиги доступа и результаты.
Лимиты архива — 64 MiB, после распаковки — 1 GiB и 10 000 файлов.
Символические ссылки и пути за пределами архива запрещены.

Зарегистрируйтесь на CodaBench и откройте **My Submissions**.
Загрузите `submission.zip` и дождитесь `Finished` со score.
Ошибка стратегии или превышение времени дают `Failed` без score.
