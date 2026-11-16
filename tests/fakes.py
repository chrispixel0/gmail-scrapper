import re
from datetime import datetime, timezone

from backend.finder import GitHubAPIError, RateLimited

PROFILES = {
    "alice": {"login": "alice", "name": "ABC Alice", "location": "XYZ", "email": "alice@gmail.com",
              "html_url": "https://github.com/alice",
              "avatar_url": "https://avatars.githubusercontent.com/u/1?v=4",
              "created_at": "2010-03-01T00:00:00Z"},
    "bob": {"login": "bob", "name": "ABC Bob", "location": "XYZ", "email": "bob@example.com",
            "html_url": "https://github.com/bob",
            "avatar_url": "https://avatars.githubusercontent.com/u/2?v=4",
            "created_at": "2012-06-15T00:00:00Z"},
    "carol": {"login": "carol", "name": "ABC Carol", "location": "XYZ", "email": None,
              "html_url": "https://github.com/carol",
              "avatar_url": "https://avatars.githubusercontent.com/u/3?v=4",
              "created_at": "2016-01-20T00:00:00Z"},
    "dave": {"login": "dave", "name": "ABC Dave", "location": "XYZ", "email": "Dave@GoogleMail.com",
             "html_url": "https://github.com/dave",
             "avatar_url": "https://avatars.githubusercontent.com/u/4?v=4",
             "created_at": "2021-09-09T00:00:00Z"},
}

_CREATED = re.compile(r"created:(\S+)\.\.(\S+)")


def _parse(ts):
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


class FakeClient:
    """
    Stands in for GitHubClient so tests never touch the network.
    Supports the created:A..B qualifier so search splitting can be tested.
    """

    def __init__(self, users=None, fail_after=None, rate_limit_once=False):
        self.users = PROFILES if users is None else users
        self.fail_after = fail_after          # raise an API error after N profile lookups
        self.rate_limit_once = rate_limit_once
        self.queries = []
        self.profile_calls = 0

    def search_page(self, query, page=1, per_page=100):
        self.queries.append(query)
        logins = sorted(self.users, key=lambda l: self.users[l]["created_at"])
        m = _CREATED.search(query)
        if m:
            start, end = _parse(m.group(1)), _parse(m.group(2))
            logins = [l for l in logins if start <= _parse(self.users[l]["created_at"]) <= end]
        chunk = logins[(page - 1) * per_page: page * per_page]
        return {"total_count": len(logins), "items": [{"login": l} for l in chunk]}

    def rate_limit(self):
        return {"limit": 5000, "remaining": 5000, "reset": 0}

    def get_user(self, login):
        if self.rate_limit_once:
            self.rate_limit_once = False
            raise RateLimited(0.05)
        if self.fail_after is not None and self.profile_calls >= self.fail_after:
            raise GitHubAPIError("GitHub rejected the token. Please enter a valid token.", "bad_token")
        self.profile_calls += 1
        return self.users[login]
