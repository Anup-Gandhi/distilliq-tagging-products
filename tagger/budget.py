"""Model prices and the per-run spending cap."""
import threading

PRICES = {  # USD per 1M tokens: (input, output)
    "claude-fable-5-1": (10.00, 50.00), "claude-fable-5": (10.00, 50.00),
    "claude-opus-5-5": (4.00, 20.00), "claude-opus-5": (5.00, 25.00), "claude-opus-4-8": (5.00, 25.00),
    "claude-sonnet-5-5": (2.00, 10.00), "claude-sonnet-5": (2.00, 10.00),
    "claude-haiku-5-5": (0.10, 0.50), "claude-haiku-4-5": (1.00, 5.00),
    "gpt-5.5": (5.00, 30.00), "gpt-4.1": (2.00, 8.00), "gpt-4.1-mini": (0.40, 1.60),
    # Gemini prices are for prompts up to 200k tokens; set prices in the secrets file to override.
    # 4-argon is $2/$10 until 30 Nov 2026, 3.8/3.6-flash $0.75/$3.75 until 31 Dec 2026; these are the later prices.
    "gemini-4-argon": (4.00, 20.00),
    "gemini-3.8-flash": (1.50, 7.50), "gemini-3.6-flash": (1.50, 7.50),
    "gemini-3.5-flash-lite": (0.30, 2.50), "gemini-3.1-flash-lite": (0.25, 1.50),
    "gemini-3.1-pro-preview": (2.00, 12.00), "gemini-3-pro-preview": (2.00, 12.00),
    "gemini-3-flash-preview": (0.50, 3.00),
    "gemini-2.5-pro": (1.25, 10.00), "gemini-2.5-flash": (0.30, 2.50), "gemini-2.5-flash-lite": (0.10, 0.40),
}
SEARCH_PRICE = 0.01                 # $10 per 1,000 searches
NON_REASONING_SEARCH_PRICE = 0.025  # OpenAI gpt-4.x: $25 per 1,000
GEMINI_SEARCH_PRICE = 0.014         # Gemini 3: $14 per 1,000 search queries
GEMINI_2_SEARCH_PRICE = 0.035       # Gemini 2.x: $35 per 1,000 grounded prompts
CACHE_WRITE, CACHE_READ = 1.25, 0.1  # multiples of the input price


class BudgetExceeded(Exception):
    pass


class Budget:
    def __init__(self, limit: float):
        self.limit = limit
        self.spent = 0.0
        self._lock = threading.Lock()

    def check(self) -> None:
        """Call before every request. Requests already in flight still finish."""
        if self.spent >= self.limit:
            raise BudgetExceeded(f"spent ${self.spent:.4f} of ${self.limit:.2f}")

    def charge(self, cost: float) -> None:
        with self._lock:
            self.spent += cost
