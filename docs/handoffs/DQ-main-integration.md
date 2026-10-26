# DQ message identity integration into GitHub main

## Scope and ancestry

This isolated branch starts at GitHub main `e07f2fa1c6a2250a6f8eb6cf98440b3b0b311e05`.
The production candidate is `66097a98e8613437f96a76ddae4e23bb7e1fd609`, based on the
separately published source snapshot `bb8937a7a712538d55af3c2af0b3cc47a917038b`.
These histories have no merge base. Main's 161 commits are retained, rather than
being merged into the production candidate or used as its deployment source.

The candidate is published unchanged at `release/2026.10.03-quality-life` and
`codex/quality-life-20261003`. NAS deployment continues to use that fixed candidate.

## Applicable fix

Main has the same incoming `MessageService`, so two identity rules are applied
there: an unidentified delivery receives a fresh identity instead of inferring
replay from text plus a second-resolution timestamp; a known source ID reused
between private and group conversations has a separate message hash. Existing
hashes remain usable for the original conversation, and a subsequent delivery
from another robot converges on the correctly scoped message.

Tests exercise both private-first and group-first collision orders. The earlier
cross-robot replay test now supplies a stable source ID, which is the evidence
required to treat two deliveries as one event.

Main has no production candidate `ImportService`, source-record model or QQNT
archive pipeline. The candidate's archive/source-alias changes cannot be applied
to that absent architecture without importing a separate product implementation;
they remain fully present in the unchanged release candidate.

## Validation

The unmodified main message-service tests passed (4 tests). After the patch:

```powershell
C:/YOKI/Codex/tianshu-peiban-bot/worktrees/quality-life-20261003/chat-audit/.runtime/dq-tests/Scripts/python.exe -m pytest tests/test_message_service.py tests/test_message_ingest_api.py tests/test_onebot11.py tests/test_capture_policy_service.py -q --tb=short
```

Result: **31 passed, 2 warnings in 1.64s**. Both warnings are existing
Starlette/httpx/AnyIO dependency deprecations. `git diff --check` passed.
No production data, migration, credentials, live QQ or NAS execution was used
for this main adaptation.
