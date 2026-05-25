from typing import List, Iterator, Optional
from dataclasses import dataclass

import requests

from models import JellyfinUser


class JellyfinAPIError(Exception):
    def __init__(self, status_code: int, message: str):
        self.status_code = status_code
        super().__init__(f"Jellyfin API error {status_code}: {message}")


@dataclass
class JellyFinServer:
    url: str
    api_key: str
    session: requests.Session

    def _headers(self) -> dict:
        return {
            "Authorization": (
                f'MediaBrowser Token="{self.api_key}", '
                f'Client="migrate-plex-to-jellyfin", Version="2.0"'
            )
        }

    def _get(self, endpoint: str, params: Optional[dict] = None) -> dict:
        try:
            r = self.session.get(
                url=f"{self.url}/{endpoint}",
                params=params or {},
                headers=self._headers(),
            )
            r.raise_for_status()
            return r.json()
        except requests.HTTPError as e:
            raise JellyfinAPIError(r.status_code, str(e)) from e
        except (ValueError, KeyError) as e:
            raise JellyfinAPIError(
                0,
                f"Invalid JSON response — check Jellyfin version compatibility ({e})",
            ) from e

    def _post(self, endpoint: str, params: Optional[dict] = None, body: Optional[dict] = None) -> requests.Response:
        try:
            r = self.session.post(
                url=f"{self.url}/{endpoint}",
                params=params or {},
                json=body,
                headers=self._headers(),
            )
            r.raise_for_status()
            return r
        except requests.HTTPError as e:
            raise JellyfinAPIError(r.status_code, str(e)) from e

    def get_users(self) -> List[JellyfinUser]:
        users = self._get("Users")
        return [JellyfinUser(id=u["Id"], name=u["Name"]) for u in users]

    def create_user(self, name: str, password: str) -> JellyfinUser:
        r = self._post("Users/New", body={"Name": name})
        data = r.json()
        user = JellyfinUser(id=data["Id"], name=data["Name"])
        self._post(f"Users/{user.id}/Password", body={"NewPw": password})
        return user

    def iter_items(self, user_id: str, page_size: int = 5000) -> Iterator[dict]:
        start = 0
        while True:
            page = self._get(
                f"Users/{user_id}/Items",
                params={
                    "Recursive": True,
                    "Fields": "MediaSources,UserData",
                    "StartIndex": start,
                    "Limit": page_size,
                },
            )
            items = page["Items"]
            yield from items
            if len(items) < page_size:
                break
            start += len(items)

    def mark_watched(self, user_id: str, item_id: str) -> None:
        self._post(f"Users/{user_id}/PlayedItems/{item_id}")

    def set_rating(self, user_id: str, item_id: str, rating: float) -> None:
        self._post(f"Users/{user_id}/Items/{item_id}/Rating", params={"rating": rating})

    def mark_favorite(self, user_id: str, item_id: str) -> None:
        self._post(f"Users/{user_id}/FavoriteItems/{item_id}")
