"""Discrete-state adaptation of Leung and Li's double optimal stopping model."""

from execution import PairedExecution


class Strategy(PairedExecution):
    def observe(self, spread, now):
        pass

    def value(self, name, direction, spread):
        policy = self.model["policies"][str(direction)]
        coordinate = (direction * spread - self.model["grid_start"]) / self.model["grid_step"]
        coordinate = min(max(coordinate, 0), len(policy[name]) - 1)
        lower = min(int(coordinate), len(policy[name]) - 2)
        weight = coordinate - lower
        return policy[name][lower] * (1 - weight) + policy[name][lower + 1] * weight

    def entry_signal(self, spread, cost):
        candidates = []
        for direction in (-1, 1):
            immediate = self.value("exit_value", direction, spread) - direction * spread - cost
            waiting = self.value("entry_wait", direction, spread)
            if immediate > self.params["profit_margin"] and immediate >= waiting:
                candidates.append((immediate, direction))
        return max(candidates)[1] if candidates else 0

    def exit_signal(self, direction, spread, cost, age):
        thresholds = self.model["policies"][str(direction)]["exit_thresholds"]
        remaining = max(0, min(int(self.model["holding_seconds"] - age), len(thresholds) - 1))
        threshold = thresholds[remaining] + cost - self.model["exit_cost"]
        return direction * spread >= threshold
