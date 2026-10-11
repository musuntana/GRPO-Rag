"""Measure the S1.1 admission scenarios through the real gateway chain.

Run from the repository root with the standard installed ``ragos`` package:

    /tmp/opencode/s1-1/venv/bin/python docs/validation/s1-1-gateway-hardening/run_counts.py

Uses the fixtures of ``tests/test_provider_admission.py``; prints observed counts.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "tests"))

import test_provider_admission as scenario  # noqa: E402
from ragos.core.errors import ProviderError  # noqa: E402
from ragos.model_gateway.router import ModelRouter  # noqa: E402


def run(label, **build):
    allow = build.pop("allow_test_stub", True)
    with pytest.MonkeyPatch.context() as mp:
        world = scenario._build_world(mp, **build)
        try:
            router = ModelRouter(world.session, allow_test_stub=allow)
            world.statements.clear()
            before = dict(router._keys._round_robin)
            outcome = "ok"
            try:
                route = router.resolve(world.context(), "chat")
                outcome = f"route provider={type(route.provider).__name__}"
            except ProviderError as error:
                outcome = json.dumps(error.to_response_content(), ensure_ascii=False)
            print(f"[{label}]")
            print(f"  outcome: {outcome}")
            print(f"  acquire_key calls: {len(world.acquire_calls)}")
            print(f"  provider SELECTs: {world.count(scenario.PROVIDER_SELECT)}")
            print(f"  key_pool SELECTs (key reads): {world.count(scenario.KEY_POOL_SELECT)}")
            print(f"  round_robin unchanged: {router._keys._round_robin == before}")
        finally:
            world.session.close()
            world.engine.dispose()


run("A disabled provider", provider_active=False)
run("A unconnected provider type", provider_type="openai")
run("A stub without opt-in", allow_test_stub=False)
run("A missing provider", with_provider=False)
run("B admitted, no active key", with_active_key=False)
run("C explicit test stub, active key")
