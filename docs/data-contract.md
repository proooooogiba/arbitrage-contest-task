# Подготовка и формат данных

Parquet-файлы, manifest и конфиг находятся на
[CodaBench](https://www.codabench.org/competitions/18505/) в разделе **Files**.

Поддерживаются один инструмент и две площадки из конфига, тип рынка `Spot=0`.
Стартовые балансы для соревнования устанавливает организатор.
Для локальных экспериментов их можно менять в конфиге.

## Подготовка

Для готового `events.jsonl` подготовка не нужна. Parquet преобразуется в этот
формат командой `prepare`.

Manifest — JSON-файл со списком источников. Пути считаются относительно manifest.

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

При одинаковом `market_ts_ns` сохраняется порядок файлов в manifest и строк в них.
Конфликтующие дубликаты нужно удалить до подготовки.

## Поддерживаемые источники

- `market_ts_ns` — время сделки или обновления стакана в модели.
- `receive_ts_ns` — момент, когда данные получены и опубликованы.

При вызове стратегии дополнительно учитываются задержки из конфига и очередь
обработки. [Правила времени](architecture.md#время-событий).

| `kind` | Содержание | `market_ts_ns` | `receive_ts_ns` |
|---|---|---|---|
| `market_frames` | Срезы стакана за окна 100 мс, схема v4 | `max(window_end_ns, ts_recv_ns, ts_publish_ns)` | Совпадает с `market_ts_ns` |
| `order_book_updates` | Обновления стакана | `server_time_ns` | `max(ts_recv_ns, ts_publish_ns)` |
| `trades` | Сделки | `ts_trade_ns` или `trade_time_ns` | `max(ts_recv_ns, ts_publish_ns)` |
| `events` | Подготовленные события | Из файла | Из файла |

Если `ts_publish_ns` отсутствует, используется `ts_recv_ns`.

Все события содержат `exchange`, `symbol`, тип рынка и временные метки.
Цены/количества в JSON — Decimal-строки, время — целые наносекунды.
Временные метки площадок должны быть согласованы в исходных данных.
`receive_ts_ns` должен быть не меньше `market_ts_ns`.

Числа float преобразуются в Decimal через `str`.

### Срезы стакана (`market_frames`)

- Цены и объёмы заданы в `bid_prices`, `bid_quantities`, `ask_prices`, `ask_quantities`.
  Из массивов длиной 10 используются `bid_count` и `ask_count` уровней.
- `valid=false`, `is_filled=true` или ненулевые `gap_count`/`resync_count` запрещают
  исполнение до следующего корректного снимка. Учитывается возраст `book_age_ns`.
- Передаются признаки окна `trade_count`, `buy_qty`, `sell_qty`, `trade_notional`,
  `trade_vwap`, его границы и показатели качества.

Данные окна доступны после его закрытия и публикации.

### Обновления стакана (`order_book_updates`)

- Уровни заданы как `[{"price":"100","qty":"5"}]`. Delta задаёт абсолютный
  объём, `qty=0` удаляет уровень.
- Первый пакет и смена `stream_epoch`/`producer_session_id` требуют полного снимка.
- Если есть `previous_update_id`, он должен совпасть с предыдущим `update_id`.
  Уменьшение `update_id` делает стакан непригодным для исполнения.
- Время получения `ts_recv_ns` берётся из метаданных доставки сообщения.

Обновления могут объединять несколько ID. Проверка полноты потока зависит от биржи.

### Сделки (`trades`)

Поля сделки — `price`, `qty`, `trade_id`, `is_buyer_maker`, временные метки
и метаданные источника.
