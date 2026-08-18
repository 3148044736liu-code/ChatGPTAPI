"""Provider safety state machine; challenges always require human recovery."""

from __future__ import annotations

import time
from dataclasses import dataclass

from src.provider_errors import ProviderError


class ProviderRiskError(ProviderError):
    code = "PROVIDER_RISK_BLOCKED"
    error_type = "provider_risk_blocked"
    status_code = 503


@dataclass
class ProviderRiskController:
    state: str = "healthy"
    reason: str = ""
    changed_at: float = 0.0
    cooldown_until: float = 0.0

    def __post_init__(self) -> None:
        if not self.changed_at:
            self.changed_at = time.time()

    def ensure_available(self) -> None:
        now = time.time()
        if self.state in {"cooling", "limited"} and now >= self.cooldown_until:
            self.resume("automatic cooldown elapsed")
        if self.state != "healthy":
            raise ProviderRiskError(f"Provider is {self.state}: {self.reason or 'manual recovery required'}")

    def transition(self, state: str, reason: str, cooldown_seconds: float = 0.0) -> None:
        self.state = state
        self.reason = reason[:500]
        self.changed_at = time.time()
        self.cooldown_until = self.changed_at + max(0.0, cooldown_seconds)

    def observe_exception(self, error: Exception) -> None:
        message = str(error).lower()
        if any(item in message for item in ("captcha", "challenge", "verify you are human", "unusual activity")):
            self.transition("challenged", "Provider challenge detected; human recovery required")
        elif any(item in message for item in ("log in", "login required", "logged out", "session expired")):
            self.transition("disabled", "Provider login is required")
        elif any(item in message for item in ("rate limit", "too many requests", "429")):
            self.transition("limited", "Provider rate limit detected", 300)
        elif any(item in message for item in ("high demand", "try again later", "temporarily unavailable")):
            self.transition("cooling", "Provider is under high demand", 120)

    def resume(self, reason: str = "administrator resumed provider") -> None:
        self.state = "healthy"
        self.reason = reason
        self.changed_at = time.time()
        self.cooldown_until = 0.0

    def snapshot(self) -> dict:
        return {
            "state": self.state,
            "reason": self.reason,
            "changed_at": self.changed_at,
            "cooldown_until": self.cooldown_until or None,
        }


risk_controller = ProviderRiskController()
