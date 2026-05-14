---
name: safety-invariants-audit
description: Run before completing ANY change to core/, risk/, exchange/, or strategy/. Verifies the 6 NUNCA VIOLAR safety rules for this real-money trading bot. Invoke whenever a diff touches order placement, error handling, credentials, network reconnection, the circuit breaker, or exchange-side confirmation.
---

# Safety Invariants Audit

Run this audit before completing ANY change to core/, risk/, exchange/, or strategy/.

## The 6 NUNCA VIOLAR Rules

Check each rule with `git diff HEAD` before confirming completion:

### Rule 1 — No removed error handling
`grep -n "except\|try:"` in modified files.
Verify: no bare `except: pass` added, no try/except blocks removed.

### Rule 2 — No hardcoded credentials
`grep -rn "api_key\s*=\s*['\"][^$]" core/ exchange/ risk/`
Must return 0 results (only `os.getenv()` allowed).

### Rule 3 — No break on network errors
Verify `exchange/client.py` still has reconnection logic.
Check: `_reconnect()` or equivalent still present.

### Rule 4 — Circuit breaker intact
`grep -n "circuit_breaker\|daily_pnl\|drawdown" risk/manager.py`
Must still enforce −3% daily drawdown limit.

### Rule 5 — place_order_safe confirms exchange-side
`grep -n "place_order_safe\|confirm" exchange/client.py`
Must verify order actually filled on exchange, not just sent.

### Rule 6 — exchange/client.py has tests
`ls tests/test_exchange_client.py`
File must exist and not be empty.

## Checklist Output Format

Print:
```
✅ Rule 1 — Error handling preserved
✅ Rule 2 — No hardcoded credentials
✅ Rule 3 — Network reconnection intact
✅ Rule 4 — Circuit breaker active
✅ Rule 5 — place_order_safe confirmed
✅ Rule 6 — Exchange tests exist

SAFE TO COMMIT ✅
```

Or:
```
❌ Rule N — [specific violation found]
DO NOT COMMIT — fix before proceeding ❌
```
