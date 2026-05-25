import secrets
import string
from typing import List, Optional

from loguru import logger
from plexapi.server import PlexServer

from jellyfin_client import JellyFinServer
from models import PlexUser, JellyfinUser


def discover_plex_users(plex: PlexServer, base_token: str) -> List[PlexUser]:
    account = plex.myPlexAccount()
    users: List[PlexUser] = [PlexUser(name=account.title, token=base_token, is_managed=False)]

    for managed in account.users():
        try:
            token = managed.get_token(plex.machineIdentifier)
            if not token:
                logger.warning(f"Could not get token for Plex user '{managed.title}' — skipping")
                continue
            users.append(PlexUser(name=managed.title, token=token, is_managed=True))
        except Exception as e:
            logger.warning(f"Failed to get token for Plex user '{managed.title}': {e} — skipping")

    return users


def _random_password(length: int = 16) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def resolve_jellyfin_user(
    jf: JellyFinServer,
    plex_name: str,
    auto_create: bool,
    dry_run: bool,
    mapped_name: Optional[str] = None,
) -> Optional[JellyfinUser]:
    jf_users = jf.get_users()
    search_name = mapped_name or plex_name

    for u in jf_users:
        if u.name == search_name:
            if mapped_name and mapped_name != plex_name:
                logger.info(f"Mapped Plex user '{plex_name}' → Jellyfin user '{u.name}'")
            return u

    for u in jf_users:
        if u.name.lower() == search_name.lower():
            if mapped_name:
                logger.info(f"Mapped Plex user '{plex_name}' → Jellyfin user '{u.name}' (case-insensitive)")
            else:
                logger.info(f"Matched Plex user '{plex_name}' → Jellyfin user '{u.name}' (case-insensitive)")
            return u

    if mapped_name:
        logger.warning(
            f"Plex user '{plex_name}' is mapped to Jellyfin user '{mapped_name}', "
            "but that Jellyfin user was not found — skipping"
        )
        return None

    if not auto_create:
        logger.warning(
            f"No Jellyfin user found for '{plex_name}' — skipping "
            f"(pass --auto-create-user to create automatically)"
        )
        return None

    if dry_run:
        logger.info(f"Would create Jellyfin user '{plex_name}' (dry run)")
        return None

    password = _random_password()
    user = jf.create_user(plex_name, password)
    print(f"\n  Created Jellyfin user '{user.name}' — initial password: {password}\n")
    logger.info(f"Created Jellyfin user '{user.name}'")
    return user
