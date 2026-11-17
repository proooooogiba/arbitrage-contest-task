class Strategy:
    def on_start(self, config, portfolio):
        return []

    def on_market_event(self, event, portfolio):
        return []

    def on_execution_report(self, report, portfolio):
        return []
