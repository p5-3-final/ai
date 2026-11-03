from pathlib import Path

from gateway_support import gateway


async def test_spec009_r22_health_is_ok_while_process_serves(contracts_dir: Path) -> None:
    async with gateway(contracts_dir, ready=lambda: False) as gw:
        response = await gw.client.get("/health")

    assert response.status_code == 200


async def test_spec009_r23_ready_after_startup_checks_and_subscription(
    contracts_dir: Path,
) -> None:
    async with gateway(contracts_dir, ready=lambda: True) as gw:
        response = await gw.client.get("/ready")

    assert response.status_code == 200


async def test_spec009_r23_not_ready_until_subscribed(contracts_dir: Path) -> None:
    async with gateway(contracts_dir, ready=lambda: False) as gw:
        response = await gw.client.get("/ready")

    assert response.status_code == 503
