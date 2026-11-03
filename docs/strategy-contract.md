# Контракт стратегии и правила

## Сабмит и API

`strategy.py` с классом `Strategy` лежит в корне директории/ZIP; рядом — веса и
модули. Рабочая директория — корень сабмита. Примеры: `examples/baseline` и
`examples/hold`. ML необязателен, зависимости фиксирует организатор в Docker-образе.

```python
class Strategy:
    def on_start(self, config, portfolio):
        return []

    def on_market_event(self, event, portfolio):
        return []

    def on_execution_report(self, report, portfolio):
        return []
```

Callbacks возвращают список заявок или `None`. Конструктор может загрузить модель;
online-обучение использует тот же бюджет и только доступную историю.

- `config`: инструмент, активы, задержки, комиссии, спецификации и риск-лимиты.
- `portfolio`: наблюдаемые остатки, например `{"binance":{"base":"250","quote":"25000"},...}`.
- `book`: `exchange`, `symbol`, `is_snapshot`, `valid`, `update_id`, `stream_epoch`,
  `market_ts_ns`, `receive_ts_ns`, `bids`, `asks`. Уровни: `[["100","5.2"], ...]`.
  Raw delta стратегия восстанавливает сама; baseline работает со snapshots.
- `trade`: `price`, `qty`, `trade_id`, `is_buyer_maker`, timestamps.
- `report`: `id`, `exchange`, `status`, `reason`, `filled_qty`, `notional`, `fee`,
  `execution_ts_ns`, `now_ns` и наблюдаемый `portfolio`.

`event.now_ns` — начало текущего callback, оно может быть позже времени события.
Деньги/количества — строки, время — целые наносекунды. Статусы: `Filled`,
`PartiallyFilled`, `Canceled`, `Rejected`; partial IOC окончателен, остаток отменён.

## Заявка

```json
{
  "id":"unique-id", "exchange":"binance", "category":"spot", "symbol":"SOLUSDT",
  "side":"Buy", "type":"Limit", "qty":"0.5", "price":"100.00",
  "time_in_force":"IOC", "reduce_only":false
}
```

Поля соответствуют `hft::Order`. `side` — `Buy`/`Sell`, ID уникален в эпизоде,
до 128 символов. Только spot Limit IOC: исполнить сразу в пределах цены, остаток
отменить. Неизвестные поля, невалидные числа, tick/lot и нарушения лимитов отклоняются.
Ноги независимы; остаток одной ноги хеджирует стратегия. Baseline — пример API,
без полноценного аварийного хеджирования.

## Параметры demo

Меняются в файле из `--config`; это условные параметры, не текущие тарифы бирж.

| Поле | Пример |
|---|---|
| `venues.<биржа>.initial_base`, `initial_quote` | По 250 SOL + 25 000 USDT на площадку; `reference_price=100`, всего 100 000 USDT |
| `taker_fee_bps` | 10 bps = 0,1%, списание в USDT с каждого исполнения |
| `order_latency_ns`, `report_latency_ns` | Binance: 5/3 мс, Bybit: 8/4 мс |
| `extra_feed_latency_ns`, `compute_latency_ns` | Дополнительный feed: 0; вычисление: 1 мс |
| `depth_fraction`, `max_order_notional` | 5% глубины на уровень; до 1000 USDT на заявку |
| `turnover_window_ns`, `max_window_turnover` | До 5000 USDT на биржу за скользящую секунду |
| `max_net_exposure` | 2000 USDT при исполнении |
| `max_orders_per_callback`, `max_orders` | 8 на callback, 100 000 на эпизод |
| `max_book_age_ns` | 500 мс с учётом возраста источника |
| `callback_timeout_s`, `max_run_seconds` | 5 с на ответ, 1800 с на запуск стратегии — реальное время |

Оконный лимит учитывает принятые заявки по limit notional, включая неисполненные.
Дополнительно размер и оборот проверяются по фактическим ценам исполнения:
заниженный sell limit лимит не обходит. Недопустимый остаток IOC отменяется.

Net exposure — суммарное изменение base относительно стартовых запасов,
умноженное на максимум `reference_price` и видимых ask-цен. Проверяется при
исполнении, объём округляется вниз до lot. Само изменение цены ликвидацию не запускает.

[Score](architecture.md#итоговый-результат) включает восстановление запасов и комиссии.
Funding равен нулю: spot без заимствования. Sharpe в MVP не вычисляется.
