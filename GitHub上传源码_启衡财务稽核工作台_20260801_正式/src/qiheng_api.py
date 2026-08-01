from __future__ import annotations

import json
import time
import threading
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Iterable


@dataclass
class QihengApiError(RuntimeError):
    status: int
    code: str
    message: str
    details: Any = None

    def __str__(self) -> str:
        return f"HTTP {self.status} [{self.code}] {self.message}"


class QihengClient:
    """Small stdlib-only client with pagination and rate-limit backoff."""

    def __init__(
        self,
        api_key: str,
        base_url: str = "http://127.0.0.1:8081",
        timeout: float = 30.0,
        max_retries: int = 5,
    ) -> None:
        if not api_key:
            raise ValueError("缺少 QIHENG_API_KEY")
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.max_retries = max_retries
        self._last_request_at = 0.0
        self._throttle_lock = threading.Lock()
        self._cache_lock = threading.Lock()
        self._me_cache: dict[str, Any] | None = None
        self._cities_cache: dict[str, Any] | None = None
        self._standards_cache: dict[str, Any] | None = None
        self._invoice_number_cache: dict[str, dict[str, Any]] = {}

    def _throttle(self) -> None:
        # Gateway limit is 10 req/s.  Keep one global limiter when batch OCR
        # uses several workers; 0.105 seconds leaves a small safety margin.
        with self._throttle_lock:
            elapsed = time.monotonic() - self._last_request_at
            if elapsed < 0.105:
                time.sleep(0.105 - elapsed)
            self._last_request_at = time.monotonic()

    def request(
        self,
        path: str,
        *,
        method: str = "GET",
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
        binary: bool = False,
    ) -> Any:
        if params:
            query = urllib.parse.urlencode(
                {k: v for k, v in params.items() if v is not None and v != ""}
            )
            path = f"{path}?{query}" if query else path
        url = f"{self.base_url}{path}"
        payload = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")

        for attempt in range(self.max_retries + 1):
            self._throttle()
            headers = {"X-Api-Key": self.api_key}
            if payload is not None:
                headers["Content-Type"] = "application/json"
            req = urllib.request.Request(url, data=payload, headers=headers, method=method)
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as res:
                    raw = res.read()
                    content_type = res.headers.get("Content-Type", "")
                    if binary or "application/json" not in content_type:
                        return raw
                    return json.loads(raw.decode("utf-8"))
            except urllib.error.HTTPError as exc:
                raw = exc.read()
                try:
                    parsed = json.loads(raw.decode("utf-8"))
                except Exception:
                    parsed = {}
                error = parsed.get("error") or {}
                api_error = QihengApiError(
                    status=exc.code,
                    code=error.get("code", "unknown"),
                    message=error.get("message", f"HTTP {exc.code}"),
                    details=error.get("details"),
                )
                if exc.code != 429 or attempt >= self.max_retries:
                    raise api_error from exc
                retry_after = float(exc.headers.get("Retry-After", "1"))
                time.sleep(max(retry_after, 0.2 * (2**attempt)))
            except urllib.error.URLError as exc:
                if attempt >= self.max_retries:
                    raise RuntimeError(f"无法连接启衡API：{exc.reason}") from exc
                time.sleep(0.3 * (2**attempt))
        raise RuntimeError("请求重试耗尽")

    def me(self) -> dict[str, Any]:
        with self._cache_lock:
            if self._me_cache is not None:
                return self._me_cache
        value = self.request("/v1/me")
        with self._cache_lock:
            self._me_cache = value
        return value

    def iter_expense_claims(self, status: str = "PENDING") -> Iterable[dict[str, Any]]:
        cursor: str | None = None
        while True:
            page = self.request(
                "/v1/expense-claims",
                params={"status": status, "limit": 200, "cursor": cursor},
            )
            yield from page.get("data", [])
            cursor = page.get("nextCursor")
            if not page.get("hasMore") or not cursor:
                return

    def get_claim(self, claim_id: str) -> dict[str, Any]:
        return self.request(f"/v1/expense-claims/{claim_id}")

    def approvals_for_claim(self, claim_id: str) -> dict[str, Any]:
        return self.request(
            "/v1/approvals", params={"refId": claim_id, "limit": 100}
        )

    def attachment_meta(self, attachment_id: str) -> dict[str, Any]:
        return self.request(f"/v1/attachments/{attachment_id}")

    def attachment_content(self, attachment_id: str) -> bytes:
        return self.request(
            f"/v1/attachments/{attachment_id}/content", binary=True
        )

    def travel_standards(self) -> dict[str, Any]:
        with self._cache_lock:
            if self._standards_cache is not None:
                return self._standards_cache
        value = self.request("/v1/travel-standards")
        with self._cache_lock:
            self._standards_cache = value
        return value

    def cities(self) -> dict[str, Any]:
        with self._cache_lock:
            if self._cities_cache is not None:
                return self._cities_cache
        value = self.request("/v1/cities")
        with self._cache_lock:
            self._cities_cache = value
        return value

    def invoices_by_number(self, invoice_no: str) -> dict[str, Any]:
        """Read-only lookup used to build the duplicate-invoice evidence set."""
        with self._cache_lock:
            if invoice_no in self._invoice_number_cache:
                return self._invoice_number_cache[invoice_no]
        value = self.request("/v1/invoices", params={"invoiceNo": invoice_no, "limit": 200})
        with self._cache_lock:
            self._invoice_number_cache[invoice_no] = value
        return value

    def iter_invoices(self, invoice_type: str | None = None) -> Iterable[dict[str, Any]]:
        """Stream invoice-ledger rows, optionally restricted to one source type."""
        cursor: str | None = None
        while True:
            page = self.request(
                "/v1/invoices",
                params={"type": invoice_type, "limit": 200, "cursor": cursor},
            )
            yield from page.get("data", [])
            cursor = page.get("nextCursor")
            if not page.get("hasMore") or not cursor:
                return

    def iter_vendors(self) -> Iterable[dict[str, Any]]:
        """Stream vendor master data needed for M3 tax-rate corroboration."""
        cursor: str | None = None
        while True:
            page = self.request("/v1/vendors", params={"limit": 200, "cursor": cursor})
            yield from page.get("data", [])
            cursor = page.get("nextCursor")
            if not page.get("hasMore") or not cursor:
                return

    def iter_receivables(self) -> Iterable[dict[str, Any]]:
        """Stream open receivables for local, human-confirmed M4 matching."""
        cursor: str | None = None
        while True:
            page = self.request("/v1/receivables", params={"limit": 200, "cursor": cursor})
            yield from page.get("data", [])
            cursor = page.get("nextCursor")
            if not page.get("hasMore") or not cursor:
                return

    def iter_receipts(self) -> Iterable[dict[str, Any]]:
        """Stream customer receipts and their posted receivable allocations."""
        cursor: str | None = None
        while True:
            page = self.request("/v1/receipts", params={"limit": 200, "cursor": cursor})
            yield from page.get("data", [])
            cursor = page.get("nextCursor")
            if not page.get("hasMore") or not cursor:
                return

    def iter_customers(self) -> Iterable[dict[str, Any]]:
        """Stream customer master data used by the receivables workbench."""
        cursor: str | None = None
        while True:
            page = self.request("/v1/customers", params={"limit": 200, "cursor": cursor})
            yield from page.get("data", [])
            cursor = page.get("nextCursor")
            if not page.get("hasMore") or not cursor:
                return

    def iter_payments(self) -> Iterable[dict[str, Any]]:
        """Stream ERP payment records for read-only M4 context.

        The current API payload contains vendor/payable fields, so these rows are
        deliberately not used as evidence for customer-receipt matching.
        """
        cursor: str | None = None
        while True:
            page = self.request("/v1/payments", params={"limit": 200, "cursor": cursor})
            yield from page.get("data", [])
            cursor = page.get("nextCursor")
            if not page.get("hasMore") or not cursor:
                return
