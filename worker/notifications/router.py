from notifications.discord import send_discord_webhook


def send_notification(channels: list[str], message: str, metadata: dict | None = None) -> dict:
    if not channels:
        raise ValueError("channels must include at least one notification channel")

    results: dict[str, dict] = {}
    for channel in channels:
        if channel == "discord":
            results[channel] = send_discord_webhook(message, metadata)
            continue
        raise ValueError(f"Unsupported notification channel '{channel}'")

    return results
