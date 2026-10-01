"""工廠庫存 Text-to-SQL 流程：問題 → 本地模型產生 SQL → 靜態檢查 → 唯讀執行（失敗就把錯誤回饋給
模型修正）→ 依查詢結果回答。

與問答共用同一套生成端與本地備援鏈（hybrid → hybrid_fallback，永遠不改走雲端）；庫存屬企業內部
資料，雲端策略（含對照組）一律回 CLOUD_CONFIDENTIAL_FORBIDDEN。SSE 事件見 shared/sse_events.md。
"""

import asyncio
import time
from collections.abc import AsyncIterator, Callable

from app.core.config import get_models_config, get_settings
from app.core.logging import log
from app.rag import text2sql
from app.rag.providers import CLOUD_STRATEGIES, ProviderUnavailable, get_provider
from app.rag.textproc import to_taiwan
from app.repositories.inventory_repo import SqlError, get_inventory_repo, prompt_schema
from app.repositories.logs_repo import get_logs_repo
from app.services import memory_guard
from app.services.chat_service import FALLBACK_CHAIN, NO_EGRESS, sse

EMPTY_ANSWER = "查無符合條件的資料。"


class GenerationUnavailable(Exception):
    """生成端與本地備援都無法使用（或已輸出一半中斷）。"""


class _Generator:
    """依策略與本地備援鏈生成；一旦改走備援，之後的修正與回答都沿用備援模型。"""

    def __init__(self, strategy: str, allow_fallback: bool):
        s = get_settings()
        self.mock = s.llm_mode == "mock" or strategy == "mock"
        self.chain = [strategy] + (FALLBACK_CHAIN.get(strategy, []) if allow_fallback else [])
        self.pos = 0
        self.reasons: list[str] = []
        self.used = "mock" if self.mock else strategy
        self.model = "mock-text2sql" if self.mock else ""
        self.input_tokens = 0
        self.output_tokens = 0

    async def stream(
        self, messages: list[dict], max_tokens: int, mock: Callable[[], str]
    ) -> AsyncIterator[str]:
        if self.mock:
            text = mock()
            self.input_tokens += sum(len(str(m["content"])) for m in messages)
            for i in range(0, len(text), 3):
                self.output_tokens += 1
                await asyncio.sleep(0.01)
                yield text[i : i + 3]
            return
        temperature = float(get_models_config().text2sql.get("temperature", 0))
        attempts = 1 + get_settings().retries
        while self.pos < len(self.chain):
            current = self.chain[self.pos]
            for attempt in range(attempts):
                produced = False
                try:
                    provider = get_provider(current)
                    provider.max_tokens, provider.temperature = max_tokens, temperature
                    async for piece in provider.stream(messages):
                        produced = True
                        yield piece
                    self.used, self.model = provider.strategy, provider.model
                    self.input_tokens += provider.usage.input_tokens
                    self.output_tokens += provider.usage.output_tokens
                    return
                except ProviderUnavailable as e:
                    if produced:
                        raise GenerationUnavailable(f"{current}：生成中斷（{e}）") from e
                    if not e.retryable or attempt == attempts - 1:
                        self.reasons.append(f"{current}：{e}")
                        break
            self.pos += 1
        raise GenerationUnavailable("；".join(self.reasons) or "沒有可用的生成端")


def _error(code: str, message: str, request_id: str) -> str:
    return sse("error", {"code": code, "message": message, "request_id": request_id})


async def ask_stream(
    question: str, request_id: str, strategy: str = "hybrid", allow_fallback: bool = True
) -> AsyncIterator[str]:
    """Text-to-SQL 只用本地 Qwen3-VL；記憶體吃緊時先釋放其他模型。"""
    events = _ask_stream(question, request_id, strategy, allow_fallback)
    async for e in memory_guard.stream("sql", {"qwen"}, events):
        yield e


async def _ask_stream(
    question: str, request_id: str, strategy: str = "hybrid", allow_fallback: bool = True
) -> AsyncIterator[str]:
    t0 = time.perf_counter()
    cfg = get_models_config().text2sql
    sql_version, answer_version = text2sql.prompt_versions()

    # 庫存屬企業內部資料：雲端策略（含對照組）一律不接受，連 schema 也不送出
    if strategy in CLOUD_STRATEGIES:
        yield _error(
            "CLOUD_CONFIDENTIAL_FORBIDDEN",
            "庫存與訂單屬企業內部資料，不送往任何雲端 API（包含對照組）",
            request_id,
        )
        return
    repo = get_inventory_repo()
    try:
        repo.ensure_built()
    except Exception as e:  # noqa: BLE001 — 建庫失敗要回報給前端，不讓串流直接斷掉
        log.error(f"庫存資料庫建立失敗：{e}")
    if not repo.path.is_file():
        yield _error(
            "INVENTORY_UNAVAILABLE",
            "庫存資料庫無法建立：" + ("；".join(repo.problems[:3]) or "找不到 kb/inventory/"),
            request_id,
        )
        return

    hints = repo.value_hints()
    messages = text2sql.build_sql_messages(question, prompt_schema(), hints, repo.as_of)
    gen = _Generator(strategy, allow_fallback)
    yield sse(
        "meta",
        {
            "request_id": request_id,
            "strategy": strategy,
            "prompt_version": sql_version,
            "as_of": repo.as_of,
        },
    )

    # 1. 產生 SQL → 檢查 → 執行；失敗就把錯誤訊息回饋給模型重寫
    max_repairs = int(cfg.get("max_repairs", 2))
    sql, result, error, first_token_ms = "", None, None, None
    history: list[dict] = []
    for attempt in range(1, max_repairs + 2):
        yield sse("attempt", {"n": attempt, "previous_error": error})
        raw = ""
        try:
            async for piece in gen.stream(
                messages,
                int(cfg.get("max_tokens", 400)),
                mock=lambda: text2sql.mock_sql(question, hints),
            ):
                if first_token_ms is None:
                    first_token_ms = round((time.perf_counter() - t0) * 1000)
                raw += piece
                yield sse("sql_token", {"text": piece})
        except GenerationUnavailable as e:
            yield _error(
                "STRATEGY_UNAVAILABLE",
                f"本地推論伺服器與備援模型都無法使用，服務暫停（不改走雲端）。{e}",
                request_id,
            )
            _log(request_id, question, strategy, gen, sql or raw, False, str(e), attempt, t0)
            return
        sql = text2sql.extract_sql(raw)
        rejected_write = False
        try:
            sql = text2sql.check_sql(sql)
            result = repo.run_readonly(
                sql, int(cfg.get("max_rows", 200)), int(cfg.get("exec_timeout_ms", 2000))
            )
            error = None
        except (text2sql.SqlRejected, SqlError) as e:
            error = str(e)
            rejected_write = getattr(e, "write", False)
        history.append({"attempt": attempt, "sql": sql, "ok": error is None, "error": error})
        yield sse("sql", history[-1])
        if error is None:
            break
        if rejected_write:
            # 使用者要求改資料：直接拒絕，不讓模型改寫成 SELECT 再「回答已修改」
            yield _error(
                "SQL_REJECTED",
                f"這個問題要求修改資料，已在執行前攔下（{error}）。庫存查詢只能讀取，"
                "沒有任何資料被修改；異動請走 ERP 的正式流程。",
                request_id,
            )
            _log(request_id, question, strategy, gen, sql, False, error, attempt, t0)
            return
        messages = [
            *messages,
            {"role": "assistant", "content": raw},
            text2sql.repair_message(error),
        ]
    sql_ms = round((time.perf_counter() - t0) * 1000)
    if result is None:
        yield _error(
            "SQL_FAILED",
            f"修正 {max_repairs} 次後仍無法產生可執行的 SQL：{error}。可以換個說法再問一次。",
            request_id,
        )
        _log(request_id, question, strategy, gen, sql, False, error, len(history), t0)
        return
    yield sse(
        "result",
        {
            "columns": result.columns,
            "rows": result.rows,
            "row_count": len(result.rows),
            "truncated": result.truncated,
            "exec_ms": result.exec_ms,
        },
    )

    # 2. 依查詢結果回答（0 筆不呼叫模型，避免編造）
    answer = ""
    if not result.rows:
        answer = EMPTY_ANSWER
        yield sse("token", {"text": answer})
    else:
        answer_messages = text2sql.build_answer_messages(
            question, sql, result.columns, result.rows, result.truncated, repo.as_of
        )
        try:
            async for piece in gen.stream(
                answer_messages,
                int(cfg.get("answer_max_tokens", 300)),
                mock=lambda: text2sql.mock_answer(result.columns, result.rows, result.truncated),
            ):
                text = to_taiwan(piece)
                answer += text
                yield sse("token", {"text": text})
        except GenerationUnavailable as e:
            # SQL 與結果已經送出，回答失敗只影響文字說明
            yield sse("token", {"text": f"（無法產生文字說明：{e}；請直接看上方查詢結果）"})

    total_ms = round((time.perf_counter() - t0) * 1000)
    done = {
        "request_id": request_id,
        "strategy_requested": strategy,
        "strategy_used": gen.used,
        "model": gen.model,
        "fallback": not gen.mock and gen.used != strategy,
        "fallback_reason": "；".join(gen.reasons) or None,
        "prompt_version": sql_version,
        "answer_prompt_version": answer_version,
        "attempts": len(history),
        "latency_ms": {
            "first_token": first_token_ms,
            "sql": sql_ms,
            "exec": result.exec_ms,
            "answer": total_ms - sql_ms,
            "total": total_ms,
        },
        "tokens": {"input": gen.input_tokens, "output": gen.output_tokens},
        "egress": NO_EGRESS,
    }
    yield sse("done", done)
    _log(
        request_id, question, strategy, gen, sql, True, None, len(history), t0,
        row_count=len(result.rows), exec_ms=result.exec_ms, sql_ms=sql_ms, answer=answer,
    )  # fmt: skip


def _log(
    request_id: str,
    question: str,
    strategy: str,
    gen: _Generator,
    sql: str,
    ok: bool,
    error: str | None,
    attempts: int,
    t0: float,
    row_count: int | None = None,
    exec_ms: int | None = None,
    sql_ms: int | None = None,
    answer: str | None = None,
) -> None:
    total_ms = round((time.perf_counter() - t0) * 1000)
    get_logs_repo().add_sql_log(
        {
            "request_id": request_id,
            "created_at": get_logs_repo().now(),
            "question": question,
            "strategy_requested": strategy,
            "strategy_used": gen.used,
            "model": gen.model,
            "prompt_version": text2sql.prompt_versions()[0],
            "sql": sql,
            "ok": int(ok),
            "error": error,
            "attempts": attempts,
            "row_count": row_count,
            "sql_ms": sql_ms,
            "exec_ms": exec_ms,
            "total_ms": total_ms,
            "input_tokens": gen.input_tokens,
            "output_tokens": gen.output_tokens,
            "answer": answer,
        }
    )
    log.info(
        "text2sql",
        extra={
            "fields": {
                "request_id": request_id,
                "strategy": strategy,
                "strategy_used": gen.used,
                "model": gen.model,
                "ok": ok,
                "attempts": attempts,
                "rows": row_count,
                "total_ms": total_ms,
                "egress": NO_EGRESS,
            }
        },
    )
