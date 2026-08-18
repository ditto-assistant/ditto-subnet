from __future__ import annotations

from types import SimpleNamespace

from ditto.api_server.dashboard_share import (
    apply_share_card,
    path_share_card,
    profile_share_card,
    shorten_hotkey,
)

_HOTKEY = "5Dvj3htjgtYyjft9C5SkDbvA4PZdvbYQRDJSKR459yGCRbPF"


def test_shorten_hotkey_keeps_prefix_and_suffix() -> None:
    assert shorten_hotkey(_HOTKEY).startswith("5Dvj3h")
    assert shorten_hotkey(_HOTKEY).endswith("bPF")


def test_path_card_uses_public_origin_and_default_image() -> None:
    card = path_share_card(
        origin="https://dittobench.ai",
        path=f"/miner/{_HOTKEY}",
        miner_id=_HOTKEY,
    )
    assert card.url == f"https://dittobench.ai/miner/{_HOTKEY}"
    assert card.image.endswith("/assets/paperditto-512.png")
    assert "SN118" in card.title
    assert card.json_ld["@type"] == "Person"


def test_profile_card_prefers_reserved_handle_and_avatar() -> None:
    profile = SimpleNamespace(
        miner_hotkey=_HOTKEY,
        name_handle=SimpleNamespace(stem="jupiter", status="reserved"),
        avatar_url=f"/api/v1/public/miners/{_HOTKEY}/avatar",
        submissions=[SimpleNamespace(), SimpleNamespace()],
        profile=SimpleNamespace(
            x_url="https://x.com/jupiter",
            github_url="https://github.com/jupiter",
            discord_handle="jupiter",
        ),
    )
    card = profile_share_card(origin="https://dittobench.ai", profile=profile)
    assert card.title.startswith("jupiter")
    assert "2 public submissions" in card.description
    assert "https://x.com/jupiter" in card.description
    assert card.image.endswith(f"/api/v1/public/miners/{_HOTKEY}/avatar")
    assert card.json_ld["sameAs"] == [
        "https://x.com/jupiter",
        "https://github.com/jupiter",
    ]


def test_apply_share_card_replaces_generic_leaderboard_tags() -> None:
    page = """<!doctype html>
<html><head>
<title>Ditto SN118 · Subnet Leaderboard</title>
<meta name="description" content="generic" />
<link rel="canonical" href="https://platform-api.heyditto.ai/" />
<meta property="og:title" content="Ditto SN118 · Subnet Leaderboard" />
<meta name="twitter:title" content="Ditto SN118 · Subnet Leaderboard" />
</head><body><div id="root"></div></body></html>
"""
    card = path_share_card(
        origin="https://dittobench.ai",
        path=f"/miner/{_HOTKEY}",
        miner_id=_HOTKEY,
    )
    out = apply_share_card(page, card)
    assert "<title>Ditto SN118 · Subnet Leaderboard</title>" not in out
    assert card.title in out
    assert 'property="og:url"' in out
    assert f"/miner/{_HOTKEY}" in out
    assert 'type="application/ld+json"' in out
    assert "Subnet Leaderboard" not in out.split("<title>")[1].split("</title>")[0]
