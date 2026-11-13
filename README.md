# Spot–Spot Arbitrage Contest

Задача — написать Python-стратегию taker–taker арбитража между двумя spot-биржами:
покупать актив из стакана одной площадки и продавать из стакана другой, зарабатывая
на разнице цен с учётом комиссий, задержек и доступной глубины.

В режиме `require_paired_orders=true` в конфиге Judge действуют правила парного входа.

- `Buy` на одной бирже и `Sell` на другой, одинаковый объём SOL.
- До принятия пары обе заявки проходят проверки цен, доступной глубины,
  балансов и лимитов.
- Одиночная заявка разрешена для хеджирования остатка. Её исполнение ограничено
  размером остатка.
- Остаток требуется закрыть за `max_unhedged_duration_ns`, по умолчанию
  **2 секунды**. Просрочка или незакрытый остаток в конце эпизода дают `Failed`
  без score.

Подробности приведены в [контракте стратегии](docs/strategy-contract.md#обязательный-парный-вход).

[Соревнование на CodaBench](https://www.codabench.org/competitions/18505/)
получило закрытую **тестовую фазу на реальных спотовых данных**. Доступ выдаёт
организатор. [Правила реального теста и готовый часовой пример](docs/real-data-test.md).
Этот репозиторий содержит evaluator, примеры стратегий и инструкции.
**Учебные Parquet-файлы распространяются через CodaBench и в Git не входят.**
Параметры demo ниже относятся к синтетическому примеру. Для реального теста
используйте `config.json` из публичного Train, содержащего также `quickstart/events.jsonl`.

## Задание и оценка

Стратегия получает события рынка и отчёты об исполнении в порядке доступности.
В ответ она отправляет заявки `Limit IOC`: исполнить сразу в пределах указанной
цены, неисполненный остаток отменить. Покупка и продажа на разных биржах
исполняются независимо: если исполнилась только одна нога, стратегию хеджирования
остатка реализует участник.

Для demo используются SOL/USDT на Binance и Bybit. На каждой площадке заранее
есть 250 SOL и 25 000 USDT; начальный номинал — 100 000 USDT при опорной цене 100.
Комиссия каждой сделки — 0,1% в USDT. Задержки и лимиты зафиксированы
в [конфиге](examples/config.json); подробнее — [торговые правила](docs/strategy-contract.md).

В конце evaluator восстанавливает исходный запас SOL **на каждой бирже** по
конечному стакану с учётом taker-комиссии:

```text
net_pnl = сумма(USDT после восстановления − стартовые USDT)
score = net_pnl / начальный номинал
```

Чем выше score, тем лучше. Стратегия без сделок получает 0. Терминальное
восстановление учитывается в score; это расчёт evaluator, а не доступный участнику
перевод между биржами. Формулы и порядок событий — в [описании оценки](docs/architecture.md).

Только spot, без плеча, заимствования, funding, переводов и пассивных заявок.
Баланс не может быть отрицательным; ликвидность, уже использованная стратегией,
повторно не доступна. Сделки участника не меняют будущую историческую траекторию.
Срезы стакана раз в 100 мс становятся доступны после закрытия/публикации окна:
это дискретная модель, не точная реконструкция биржи внутри окна.

## Локальный запуск

Нужны Git и Python 3.9+; для соответствия среде стратегии рекомендуется Python 3.11.
Команды рассчитаны на Linux/macOS; в Windows используйте WSL2. Для Docker-запуска
нужен работающий Docker Engine / Docker Desktop.

```bash
git clone https://github.com/proooooogiba/arbitrage-contest-task.git
cd arbitrage-contest-task
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -c requirements.lock -e .

# Генератор создаёт только синтетическую историю и примеры
.venv/bin/python -m spot_contest demo --output data/demo
cp -R examples/baseline my_strategy

# Быстрая отладка собственного кода без Docker
.venv/bin/python -m spot_contest evaluate \
  --config data/demo/config.json --data data/demo/events.jsonl \
  --submission my_strategy --output runs/debug-001 --runner subprocess
```

`subprocess` запускает код без файловой и сетевой изоляции; используйте его для
своей стратегии. Перед отправкой проверьте её в Docker, как при серверной оценке:

```bash
docker build -f Dockerfile.strategy -t spot-contest-strategy:0.1.0 .
.venv/bin/python -m spot_contest evaluate \
  --config data/demo/config.json --data data/demo/events.jsonl \
  --submission my_strategy --output runs/docker-001
```

Неизменённый baseline на demo: `net_pnl=2.528435`, `score=0.00002528435`,
26 исполненных заявок. Это проверка интеграции, а не оценка качества на реальном
рынке. Для каждого запуска выбирайте новый каталог `--output`; существующий
результат не перезаписывается. Повторно создавать уже заполненный `data/demo` не нужно.

| Файл результата | Содержание |
|---|---|
| `runs/<запуск>/scores.json` | Score, PnL, комиссии и другие метрики |
| `runs/<запуск>/result.json` | Балансы и терминальное восстановление |
| `runs/<запуск>/audit.jsonl` | События, заявки и исполнения |
| `runs/<запуск>/run.json` | Статус, правила и хеши запуска; причина ошибки при `failed` |

## Данные с CodaBench

Учебную историю, manifest и соответствующий конфиг скачивайте из раздела Files
соревнования на CodaBench после их публикации. Храните их локально в `data/train/`.
Parquet, архивы данных и локальные результаты исключены из Git. Закрытый тест
участникам не выдаётся. Названия файлов ниже — пример структуры скачанного комплекта.

Для подготовки Parquet установите дополнительную библиотеку:

```bash
.venv/bin/python -m pip install -c requirements.lock -e '.[parquet]'
.venv/bin/python -m spot_contest prepare \
  --manifest data/train/sources.json --output data/train/events.jsonl
.venv/bin/python -m spot_contest validate \
  --config data/train/config.json --data data/train/events.jsonl
.venv/bin/python -m spot_contest evaluate \
  --config data/train/config.json --data data/train/events.jsonl \
  --submission my_strategy --output runs/train-001
```

Если комплект уже содержит подготовленный `events.jsonl`, шаг `prepare` не нужен.
Формат manifest и полей описан в [контракте данных](docs/data-contract.md).
Изменение локального конфига полезно для экспериментов, но сервер применяет
собственные опубликованные правила; конфиг из ZIP участника их не переопределяет.

## Стратегия и отправка

Файл `my_strategy/strategy.py` должен определять класс `Strategy` с методами
`on_start(config, portfolio)`, `on_market_event(event, portfolio)` и
`on_execution_report(report, portfolio)`. Каждый callback возвращает список
заявок или `None`. Цены и количества передаются строками; время — в наносекундах.
Полный API и пример заявки — в [контракте стратегии](docs/strategy-contract.md).

- [baseline](examples/baseline/strategy.py) — простой арбитраж; полноценное
  хеджирование остатка участник добавляет сам.
- [hold](examples/hold/strategy.py) — стратегия без сделок.
- [cross_spread](examples/cross_spread/strategy.py) — диагностический арбитраж
  сырого положительного спреда без фильтра комиссий, для проверки издержек.
- [broken](examples/broken/strategy.py) — намеренная ошибка для проверки обработки сбоев.

Среда стратегии — Python 3.11 и стандартная библиотека. Установка пакетов во время
оценки и доступ к сети отсутствуют. Дополнительные библиотеки должны быть заранее
добавлены организатором в общий образ; локально установленный пакет сам в него не попадёт.

Соберите ZIP так, чтобы `strategy.py` лежал в корне, а не внутри `my_strategy/`:

```bash
cd my_strategy
../.venv/bin/python -m zipfile -c ../submission.zip strategy.py
cd ..
.venv/bin/python -m zipfile -l submission.zip
```

При наличии собственных модулей или весов добавьте их файлы/каталоги в команду
архивации после `strategy.py`, сохранив относительные пути. Не включайте evaluator,
данные, `.venv`, конфиги доступа и результаты. Лимиты: ZIP до 64 MiB, после распаковки
до 1 GiB и 10 000 файлов; symlink и пути за пределами архива запрещены.

На CodaBench откройте **My Submissions**, загрузите `submission.zip` и дождитесь
`Finished` со score. Ошибка стратегии или превышение времени дают `Failed` без
числового score. Сервер оценивает код на своём фиксированном наборе данных.

## Проверка evaluator

```bash
.venv/bin/python -m unittest discover -s tests -v
```

Evaluator соответствует версии `0.1.0` текущего smoke test. Официальная среда
оценки — Linux; хеши данных, правил, кода и ID Docker-образа сохраняются в `run.json`.
