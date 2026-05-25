import pytest
import requests
from unittest.mock import MagicMock
from requests.exceptions import HTTPError

from jellyfin_client import JellyFinServer, JellyfinAPIError
from models import JellyfinUser


@pytest.fixture
def session():
    return MagicMock(spec=requests.Session)


@pytest.fixture
def client(session):
    return JellyFinServer(url="http://jf.test", api_key="secret", session=session)


def ok_response(json_data):
    r = MagicMock()
    r.status_code = 200
    r.ok = True
    r.raise_for_status.return_value = None
    r.json.return_value = json_data
    return r


def error_response(status_code):
    r = MagicMock()
    r.status_code = status_code
    r.ok = False
    r.raise_for_status.side_effect = HTTPError(response=r)
    return r


class TestHeaders:
    def test_contains_token(self, client):
        h = client._headers()
        assert 'MediaBrowser Token="secret"' in h["Authorization"]

    def test_contains_client_name(self, client):
        h = client._headers()
        assert "migrate-plex-to-jellyfin" in h["Authorization"]


class TestGet:
    def test_raises_api_error_on_4xx(self, client, session):
        session.get.return_value = error_response(401)
        with pytest.raises(JellyfinAPIError) as exc:
            client._get("Users")
        assert exc.value.status_code == 401

    def test_raises_api_error_on_bad_json(self, client, session):
        r = MagicMock()
        r.status_code = 200
        r.raise_for_status.return_value = None
        r.json.side_effect = ValueError("bad json")
        session.get.return_value = r
        with pytest.raises(JellyfinAPIError) as exc:
            client._get("Users")
        assert "JSON" in str(exc.value) or "json" in str(exc.value).lower()

    def test_returns_json_on_success(self, client, session):
        session.get.return_value = ok_response({"foo": "bar"})
        assert client._get("anything") == {"foo": "bar"}

    def test_sends_auth_header(self, client, session):
        session.get.return_value = ok_response({})
        client._get("Users")
        call_kwargs = session.get.call_args.kwargs
        assert "Authorization" in call_kwargs["headers"]


class TestIterItems:
    def test_yields_items_from_single_page(self, client, session):
        items = [{"Id": "a"}, {"Id": "b"}]
        session.get.return_value = ok_response({"Items": items})
        assert list(client.iter_items("u1", page_size=100)) == items

    def test_fetches_second_page_when_first_is_full(self, client, session):
        page1 = [{"Id": str(i)} for i in range(3)]
        page2 = [{"Id": "last"}]
        session.get.side_effect = [
            ok_response({"Items": page1}),
            ok_response({"Items": page2}),
        ]
        result = list(client.iter_items("u1", page_size=3))
        assert len(result) == 4
        assert session.get.call_count == 2

    def test_stops_when_page_smaller_than_limit(self, client, session):
        session.get.return_value = ok_response({"Items": [{"Id": "x"}]})
        list(client.iter_items("u1", page_size=100))
        assert session.get.call_count == 1

    def test_passes_start_index_on_second_call(self, client, session):
        page1 = [{"Id": str(i)} for i in range(2)]
        page2 = [{"Id": "done"}]
        session.get.side_effect = [
            ok_response({"Items": page1}),
            ok_response({"Items": page2}),
        ]
        list(client.iter_items("u1", page_size=2))
        second_call_params = session.get.call_args_list[1].kwargs["params"]
        assert second_call_params["StartIndex"] == 2


class TestGetUsers:
    def test_returns_typed_jellyfin_users(self, client, session):
        session.get.return_value = ok_response([
            {"Id": "1", "Name": "alice"},
            {"Id": "2", "Name": "bob"},
        ])
        users = client.get_users()
        assert users == [JellyfinUser(id="1", name="alice"), JellyfinUser(id="2", name="bob")]


class TestCreateUser:
    def test_calls_new_endpoint_then_password_endpoint(self, client, session):
        session.post.side_effect = [
            ok_response({"Id": "new_id", "Name": "carol"}),
            ok_response({}),
        ]
        user = client.create_user("carol", "s3cr3t")
        assert user == JellyfinUser(id="new_id", name="carol")
        assert session.post.call_count == 2
        first_url = session.post.call_args_list[0].kwargs["url"]
        second_url = session.post.call_args_list[1].kwargs["url"]
        assert "Users/New" in first_url
        assert "new_id" in second_url
        assert "Password" in second_url


class TestMarkWatched:
    def test_posts_to_played_items_endpoint(self, client, session):
        session.post.return_value = ok_response({})
        client.mark_watched("uid", "iid")
        url = session.post.call_args.kwargs["url"]
        assert "Users/uid/PlayedItems/iid" in url

    def test_sends_date_played_when_provided(self, client, session):
        session.post.return_value = ok_response({})
        client.mark_watched("uid", "iid", date_played="2023-10-15T14:30:00.000000Z")
        params = session.post.call_args.kwargs.get("params")
        assert params == {"DatePlayed": "2023-10-15T14:30:00.000000Z"}

    def test_raises_on_failure(self, client, session):
        session.post.return_value = error_response(500)
        with pytest.raises(JellyfinAPIError):
            client.mark_watched("uid", "iid")


class TestSetPlaybackPosition:
    def test_posts_to_user_item_data_endpoint(self, client, session):
        session.post.return_value = ok_response({})
        client.set_playback_position("uid", "iid", 123450000)
        call_kwargs = session.post.call_args.kwargs
        assert "UserItems/iid/UserData" in call_kwargs["url"]
        assert call_kwargs["params"] == {"userId": "uid"}
        assert call_kwargs["json"] == {"PlaybackPositionTicks": 123450000}


class TestSetRating:
    def test_posts_to_rating_endpoint(self, client, session):
        session.post.return_value = ok_response({})
        client.set_rating("uid", "iid", 8.0)
        url = session.post.call_args.kwargs["url"]
        assert "Users/uid/Items/iid/Rating" in url


class TestMarkFavorite:
    def test_posts_to_favorite_items_endpoint(self, client, session):
        session.post.return_value = ok_response({})
        client.mark_favorite("uid", "iid")
        url = session.post.call_args.kwargs["url"]
        assert "Users/uid/FavoriteItems/iid" in url
