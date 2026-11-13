# Spot–Spot Arbitrage Contest

Напишите Python-стратегию taker–taker арбитража между двумя spot-биржами.
Покупайте актив на одной площадке и продавайте на другой с учётом комиссий,
задержек и доступной глубины.

Для открытия сделки нужна пара заявок.

- Отправляйте `Buy` и `Sell` одинакового объёма на разных биржах.
- Одиночная заявка разрешена для хеджирования остатка в пределах его размера.
- Закройте остаток за `max_unhedged_duration_ns`, по умолчанию **2 секунды**.
  Просрочка или остаток в конце эпизода дают `Failed` без score.

Подробности приведены в [контракте стратегии](docs/strategy-contract.md#обязательный-парный-вход).

В репозитории находятся программа локальной оценки (`spot_contest`), примеры
стратегий и инструкции.

Parquet-файлы, manifest и конфиг находятся в разделе **Files** на CodaBench.
[Файлы соревнования](https://www.codabench.org/competitions/18505/),
[правила теста](docs/real-data-test.md).

## Задание и оценка

Стратегия получает события рынка и отчёты об исполнении в порядке доступности.
В ответ она отправляет заявки `Limit IOC`. Они исполняются сразу в пределах
указанной цены, оставшийся объём отменяется.

Ноги исполняются независимо. Если объёмы покупки и продажи различаются,
остаток хеджирует стратегия участника.

В синтетическом примере `demo` используются SOL/USDT на Binance и Bybit.
На каждой площадке заранее есть 250 SOL и 25 000 USDT; начальный номинал —
100 000 USDT при опорной цене 100.

Комиссия каждой сделки — 0,1% в USDT. Задержки и лимиты зафиксированы
в конфиге. [Конфиг demo](examples/config.json), [торговые правила](docs/strategy-contract.md).

В конце оценки исходный запас SOL восстанавливается **на каждой бирже** по
конечному стакану с учётом taker-комиссии.

```text
net_pnl = сумма(USDT после восстановления − стартовые USDT)
score = net_pnl / начальный номинал
```

Чем выше score, тем лучше. Стратегия без сделок получает 0.
Восстановление запасов учитывается при расчёте результата. [Описание оценки](docs/architecture.md).

Только spot, без плеча, заимствования, funding, переводов и пассивных заявок.
Баланс должен оставаться неотрицательным. Исполненные объёмы вычитаются из
доступной ликвидности. Сделки участника не меняют дальнейшую историю рынка.

Срезы стакана раз в 100 мс доступны после закрытия и публикации окна.

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
cp -R examples/baseline my_strategy

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

- [baseline](examples/baseline/strategy.py) — простой арбитраж без полноценного
  хеджирования остатка.
- [hold](examples/hold/strategy.py) — стратегия без сделок.
- [cross_spread](examples/cross_spread/strategy.py) — арбитраж положительного
  спреда без учёта комиссий в сигнале.
- [broken](examples/broken/strategy.py) — намеренная ошибка для проверки обработки сбоев.

Среда стратегии — Python 3.11 и стандартная библиотека. Доступа к сети и установки
пакетов во время оценки нет. Дополнительные библиотеки доступны, только если
организатор заранее включил их в среду оценки.

Поместите `strategy.py` в корень ZIP.

```bash
cd my_strategy
../.venv/bin/python -m zipfile -c ../submission.zip strategy.py
cd ..
.venv/bin/python -m zipfile -l submission.zip
```

При наличии собственных модулей или весов добавьте их файлы/каталоги в команду
архивации после `strategy.py`, сохранив относительные пути.

Исключите из архива evaluator, данные, `.venv`, конфиги доступа и результаты.
Лимиты архива — 64 MiB, после распаковки — 1 GiB и 10 000 файлов.
Символические ссылки и пути за пределами архива запрещены.

Зарегистрируйтесь на CodaBench и откройте **My Submissions**.
Загрузите `submission.zip` и дождитесь `Finished` со score.
Ошибка стратегии или превышение времени дают `Failed` без score.
