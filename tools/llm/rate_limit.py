"""One rolling RPM/TPM budget shared by all LLM clients in the app process."""
import asyncio
from collections import deque
from dataclasses import dataclass
from weakref import WeakKeyDictionary

from config.settings import settings


@dataclass(eq=False)
class TokenReservation:
    timestamp: float
    tokens: int
    pending: bool = True


class LLMRateLimiter:
    """Reserve input estimate + max output before sending a request.

    RPM counts admissions in a rolling window. TPM includes unfinished
    reservations and completed usage in the last window. Errors without usage
    retain their reservation, since the provider may already have billed them.
    """

    def __init__(self, rpm: int, tpm: int, window_seconds: float = 60.0):
        if rpm < 1 or tpm < 1 or window_seconds <= 0:
            raise ValueError("RPM, TPM and window_seconds must be positive")
        self.rpm = rpm
        self.tpm = tpm
        self.window = window_seconds
        self._requests = deque()
        self._tokens = []
        self._condition = asyncio.Condition()

    async def acquire(self, tokens: int) -> TokenReservation:
        if tokens < 1 or tokens > self.tpm:
            raise ValueError(
                f"单次请求预留 {tokens} tokens，超出 LLM_TPM_LIMIT={self.tpm}；"
                "请缩短输入、降低 max_tokens 或调整 TPM 配额"
            )
        loop = asyncio.get_running_loop()
        async with self._condition:
            while True:
                now = loop.time()
                while self._requests and now - self._requests[0] >= self.window:
                    self._requests.popleft()
                self._tokens = [r for r in self._tokens
                                if r.pending or now - r.timestamp < self.window]
                # ponytail: linear sum at small RPM; maintain a running total if needed.
                if (len(self._requests) < self.rpm
                        and sum(r.tokens for r in self._tokens) + tokens <= self.tpm):
                    reservation = TokenReservation(now, tokens)
                    self._requests.append(now)
                    self._tokens.append(reservation)
                    return reservation

                expirations = [t + self.window for t in self._requests]
                expirations.extend(r.timestamp + self.window for r in self._tokens
                                   if not r.pending)
                if not expirations:
                    # All token capacity is held by unfinished requests.
                    await self._condition.wait()
                    continue
                delay = max(0.001, min(expirations) - now)
                try:
                    await asyncio.wait_for(self._condition.wait(), timeout=delay)
                except asyncio.TimeoutError:
                    pass

    async def finish(self, reservation: TokenReservation, actual_tokens=None) -> None:
        async with self._condition:
            if actual_tokens is not None:
                reservation.tokens = max(0, int(actual_tokens))
            reservation.pending = False
            reservation.timestamp = asyncio.get_running_loop().time()
            self._condition.notify_all()


_limiters = WeakKeyDictionary()


def get_llm_rate_limiter() -> LLMRateLimiter:
    """Process-local budget, shared across workflows, clients and providers."""
    loop = asyncio.get_running_loop()
    if loop not in _limiters:
        _limiters[loop] = LLMRateLimiter(settings.LLM_RPM_LIMIT, settings.LLM_TPM_LIMIT)
    return _limiters[loop]
