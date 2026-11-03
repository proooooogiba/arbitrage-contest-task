# Подготовка и формат данных

Адаптеры поддерживают описанные ниже JSONL/Parquet-схемы. Учебные Parquet-файлы,
manifest и конфиг распространяются через CodaBench, отдельно от репозитория.
Принимаются один инструмент и две
площадки из config; `Spot=0`, futures отклоняются. Настоящие балансы счетов не
загружаются: виртуальные остатки задаются в правилах.

## Подготовка

Распакуйте исходные архивы и перечислите JSONL/Parquet в manifest.
Пути считаются относительно manifest; для нескольких дней добавьте их файлы:

```json
{"sources":[
  {"kind":"market_frames","path":"binance/frames.parquet"},
  {"kind":"market_frames","path":"bybit/frames.parquet"},
  {"kind":"trades","path":"binance/trades.parquet"},
  {"kind":"trades","path":"bybit/trades.parquet"}
]}
```

```bash
.venv/bin/python -m spot_contest prepare --manifest data/sources.json --output data/events.jsonl
.venv/bin/python -m spot_contest validate --config examples/config.json --data data/events.jsonl
```

`prepare` читает данные пакетами, сортирует на диске и сохраняет SHA-256 источников.
Порядок — `market_ts_ns`, затем исходный порядок строк: при одинаковом времени
связанных snapshot/delta порядок файлов должен отражать порядок событий.
Конфликтующие дубликаты удаляются до подготовки. Evaluator читает готовый JSONL;
скачивания S3 и распаковки tar в нём нет.

## Поддержанные источники

| `kind` | Содержание и время |
|---|---|
| `market_frames` | Окна v4, snapshots L2: `market_ts_ns = receive_ts_ns = max(window_end_ns, ts_recv_ns, ts_publish_ns)` |
| `order_book_updates` | `OrderBookUpdate`: рынок — `server_time_ns`, доставка — `ts_recv_ns` с учётом публикации |
| `trades` | Trades v1 / C++ Trade: рынок — `ts_trade_ns` / `trade_time_ns`, доставка — `max(ts_recv_ns, ts_publish_ns)` |
| `events` | Нормализованные события этого проекта |

Все события содержат `exchange`, `symbol`, тип рынка и временные метки.
Цены/количества в JSON — Decimal-строки, время — целые наносекунды.
Исходные float преобразуются через `str`: потерянную точность это не восстанавливает.
Receive time раньше market time отклоняется; часы площадок согласуются до экспорта.

**Frames:** поля стакана — `bid_count`, `ask_count`, `bid_prices`, `bid_quantities`,
`ask_prices`, `ask_quantities`; из массивов длиной 10 читаются только активные уровни.
`valid=false`, `is_filled=true`, ненулевые `gap_count`/`resync_count` запрещают
исполнение до следующего корректного snapshot. Также учитывается `book_age_ns`.
Передаются закрытые признаки `trade_count`, `buy_qty`, `sell_qty`, `trade_notional`,
`trade_vwap`, границы окна и качество. Между 100 ms срезами рынок неизвестен;
содержимое окна нельзя использовать раньше его закрытия/публикации.

**OrderBookUpdate:** уровни — `[{"price":"100","qty":"5"}]`; delta задаёт новое
абсолютное количество, `qty=0` удаляет уровень. Первый пакет и смена
`stream_epoch`/`producer_session_id` требуют snapshot. Если есть `previous_update_id`,
он должен совпасть с прошлым ID; уменьшение `update_id` инвалидирует стакан.
Receive timestamp нужно добавить из транспортного envelope — в C++-модели его нет.
Проверка полноты потока зависит от биржи: разность ID больше единицы сама по себе
не доказывает пропуск, поскольку обновления могут агрегироваться.

**Trade:** `price`, `qty`, `trade_id`, `is_buyer_maker`, timestamps и producer metadata.

## Разбиения

Текущий smoke test использует синтетическую историю, создаваемую командой `demo`.
Для основного конкурса организатор публикует учебную историю на CodaBench;
закрытый test использует ту же схему. Конкретные периоды validation/final,
разрывы между выборками и агрегация эпизодов будут указаны в правилах CodaBench.
CLI оценивает один эпизод; каждый новый эпизод стартует с заданными в конфиге остатками.
