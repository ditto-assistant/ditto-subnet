from ditto.api_server.coding_certification_canary import public_certification_canary


def test_public_certification_canary_is_canonical_and_weight_ineligible() -> None:
    canary = public_certification_canary()
    assert len(canary.canary_manifest_sha256) == 64
    assert canary.inference_policy_sha256 == (
        "6dd79225817b56ebf155f8344cd5faf752c8dd57802b21d6d2cbbae9cc2ff0b4"
    )
    assert canary.canary_manifest_sha256 == (
        "cb608113db0cc31001fe0a7294854453061f9e85d1471520100ce99eca97a903"
    )
