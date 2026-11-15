"""Three-regime empirical switching, adapted from Ngo and Pham's formulation."""

from bisect import bisect_right

from execution import PairedExecution


class Strategy(PairedExecution):
    def observe(self, spread, now):
        pass

    def value(self, position, spread, seconds):
        grid = self.model["grid"]
        values = self.model["continuation"][seconds][position + 1]
        lower = min(max(bisect_right(grid, spread) - 1, 0), len(grid) - 2)
        coordinate = min(max(spread, grid[0]), grid[-1])
        weight = (coordinate - grid[lower]) / (grid[lower + 1] - grid[lower])
        value = values[lower] * (1 - weight) + values[lower + 1] * weight
        # Outside the training support, preserve the cash value of inventory.
        return value + position * (spread - coordinate)

    def entry_signal(self, spread, cost):
        seconds = self.model["holding_seconds"]
        waiting = self.value(0, spread, seconds)
        candidates = []
        for direction in (-1, 1):
            entering = -direction * spread - cost + self.value(direction, spread, seconds)
            if entering > waiting + self.params["profit_margin"]:
                candidates.append((entering, direction))
        return max(candidates)[1] if candidates else 0

    def exit_signal(self, direction, spread, cost, age):
        seconds = max(0, int(self.model["holding_seconds"] - age))
        closing = direction * spread - cost + self.value(0, spread, seconds)
        return closing >= self.value(direction, spread, seconds)
