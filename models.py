from dataclasses import dataclass


@dataclass
class PlexUser:
    name: str
    token: str
    is_managed: bool


@dataclass
class JellyfinUser:
    id: str
    name: str


@dataclass
class MigrationStats:
    marked: int = 0
    missing: int = 0
    skipped: int = 0
    ratings_set: int = 0
    favorites_set: int = 0
