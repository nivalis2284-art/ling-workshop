# Debug Session: DEBUG-001 (Ruff Linter Clean-up)

## Symptom
`ruff check src tests scripts` 报告 3 处告警/错误：
1. `src/lingnexus/tasks/execution.py:32:9`: F841 局部变量 `records` 被赋值但从未使用
2. `tests/test_context.py:3:54`: F401 `SourceInvalid` 导入但未在模块中使用
3. `tests/test_memory_maintenance.py:162:12`: C401 `set(...)` 中的生成器表达式可改写为集合推导式 `{...}`

**触发时机:** 在第一阶段移除情境（Scope）清洗改动后，静态代码检查触发
**预期结果:** `ruff check src tests scripts` 报告 0 errors, 0 warnings
**实际结果:** 3 个代码风格/静态规范报错

## Hypotheses

| # | Hypothesis | Likelihood | Status |
|---|---|---|---|
| 1 | `execution.py:32` 中的 `records` 是移除 scope 校验后的遗留赋值，直接保留 `validate_refs` 调用并去除赋值即可保留断言校验 | 100% | CONFIRMED |
| 2 | `tests/test_context.py:3` 中的 `SourceInvalid` 是删除情境用例后的无用引用，移除该导入即可 | 100% | CONFIRMED |
| 3 | `tests/test_memory_maintenance.py:162` 将 `set(r["id"] for ...)` 改写为 `{r["id"] for ...}` 集合推导式即可 | 100% | CONFIRMED |

## Resolution

**Root Cause:**
1. `execution.py:32`: 在此前移除 scope 校验后，未同步删除已无用的局部变量赋值 `records =`，触发 F841。
2. `tests/test_context.py:3`: 删除了测试用例中的情境防护断言后，遗留了 `SourceInvalid` 异常类导入，触发 F401。
3. `tests/test_memory_maintenance.py:162`: `set(...)` 生成器未遵循 Ruff 推荐的直接集合推导式，触发 C401。

**Fix:**
1. [`src/lingnexus/tasks/execution.py`](file:///h:/文档/LingNexus/Ling/src/lingnexus/tasks/execution.py): 移除 `records =` 局部赋值，保留底层校验副作用。
2. [`tests/test_context.py`](file:///h:/文档/LingNexus/Ling/tests/test_context.py): 移除未使用的 `SourceInvalid` 导入。
3. [`tests/test_memory_maintenance.py`](file:///h:/文档/LingNexus/Ling/tests/test_memory_maintenance.py): 将 `set(r["id"] for ...)` 改为 `{r["id"] for ...}`。

**Verification:**
- `ruff check src tests scripts`: All checks passed! (0 errors, 0 warnings)
- `pytest tests/test_context.py tests/test_memory_maintenance.py`: 32 passed in 7.82s.
- `pytest tests/test_task_memory_fence.py`: 63 passed in 38.42s.
- `pyright`: 0 errors, 0 warnings, 0 informations.

**Status:** RESOLVED ✓
