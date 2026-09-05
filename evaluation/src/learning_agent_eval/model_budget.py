"""Cross-process prepaid Hy3 budget; missing usage keeps the full reservation."""

from __future__ import annotations

import fcntl
import json
import os
from contextlib import contextmanager
from pathlib import Path

# CNY millionths, using the non-cached official prices checked on 2026-09-05.
INPUT_LIMIT = 196608
OUTPUT_LIMIT = 16000
INPUT_RATE = 1
OUTPUT_RATE = 4
RESERVATION = INPUT_LIMIT * INPUT_RATE + OUTPUT_LIMIT * OUTPUT_RATE


class ModelBudgetExceeded(RuntimeError):
    pass


class ModelBudget:
    def __init__(self, path: str | Path):
        self.path = Path(path).absolute()
        if self.path.is_symlink() or self.path.resolve() != self.path:
            raise ValueError("budget ledger must use a canonical non-symlink path")

    @classmethod
    def create(cls, path: Path, *, limit_micro_cny: int) -> ModelBudget:
        if type(limit_micro_cny) is not int or limit_micro_cny < RESERVATION:
            raise ValueError("budget must cover at least one worst-case request")
        budget = cls(path)
        with path.open("x", encoding="utf-8") as handle:
            os.chmod(path, 0o600)
            json.dump({
                "version": "hy3-prepaid-budget-v1", "limit_micro_cny": limit_micro_cny,
                "price_date": "2026-09-05", "input_rate": INPUT_RATE,
                "output_rate": OUTPUT_RATE, "input_limit": INPUT_LIMIT,
                "output_limit": OUTPUT_LIMIT, "blocked": False, "requests": [],
            }, handle, sort_keys=True)
        return budget

    @contextmanager
    def _locked(self):
        # No automatic recreation/reset. A corrupt or missing ledger fails closed.
        descriptor = os.open(self.path, os.O_RDWR | os.O_NOFOLLOW)
        with os.fdopen(descriptor, "r+", encoding="utf-8") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            document = json.load(handle)
            if (document["version"] != "hy3-prepaid-budget-v1"
                    or document["input_rate"] != INPUT_RATE
                    or document["output_rate"] != OUTPUT_RATE
                    or document["input_limit"] != INPUT_LIMIT
                    or document["output_limit"] != OUTPUT_LIMIT):
                raise ValueError("unsupported budget pricing policy")
            yield document
            handle.seek(0)
            json.dump(document, handle, sort_keys=True)
            handle.truncate()
            handle.flush()
            os.fsync(handle.fileno())

    def reserve(self, *, scope: str, call_id: str) -> int:
        with self._locked() as document:
            charged = sum(item["charged_micro_cny"] for item in document["requests"])
            if document["blocked"] or charged + RESERVATION > document["limit_micro_cny"]:
                raise ModelBudgetExceeded("shared prepaid Hy3 budget exhausted")
            ticket = len(document["requests"]) + 1
            document["requests"].append({
                "ticket": ticket, "scope": scope, "call_id": call_id,
                "charged_micro_cny": RESERVATION, "status": "reserved",
                "token_usage": None,
            })
        return ticket

    def settle(self, ticket: int, usage: dict | None) -> None:
        with self._locked() as document:
            item = document["requests"][ticket - 1]
            if item["ticket"] != ticket or item["status"] != "reserved":
                raise ValueError("budget ticket has already been settled")
            if usage is None:
                item["status"] = "unknown_usage_reserved"
                return
            prompt, completion, total = (
                usage.get(key) for key in ("prompt_tokens", "completion_tokens", "total_tokens")
            )
            if (not all(type(value) is int and value >= 0 for value in (prompt, completion, total))
                    or prompt > INPUT_LIMIT or completion > OUTPUT_LIMIT
                    or total != prompt + completion):
                document["blocked"] = True
                item["status"] = "invalid_usage_reserved"
                return
            item.update(
                status="settled", token_usage=usage,
                charged_micro_cny=prompt * INPUT_RATE + completion * OUTPUT_RATE,
            )

    def summary(self) -> dict:
        with self._locked() as document:
            return {
                "limit_micro_cny": document["limit_micro_cny"],
                "charged_micro_cny": sum(i["charged_micro_cny"] for i in document["requests"]),
                "requests": len(document["requests"]), "blocked": document["blocked"],
                "unsettled": sum(i["status"] != "settled" for i in document["requests"]),
            }
