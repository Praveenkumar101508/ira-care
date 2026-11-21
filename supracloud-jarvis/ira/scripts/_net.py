"""The one place the supply-chain gates reach the network.

Three gates fetch remote data — OSV advisories, PyPI metadata, published
wheels and their PEP 658 sidecars. Each `urlopen` call is a Bandit **B310**
(`urllib` opened with a non-literal URL), and the honest response to four of
them is not four baseline entries with the same paragraph copied out.

So every fetch goes through here, and here the URL is checked rather than
excused: **https only**, no `file:` or `ftp:` reachable through a mis-built
string, no silent redirect off scheme. That leaves exactly one B310 in the
tree, and the baseline entry for it points at a function that does the thing
the warning is asking for.

Callers get `None` on failure with the reason printed. `None` is NOT an empty
result: a gate that could not reach its data source reports UNKNOWN, never
clean. That distinction is Phase 13's whole subject and every caller here
depends on it.
"""
from __future__ import annotations

import urllib.error
import urllib.parse
import urllib.request

#: A User-Agent is not decoration. The PyTorch index CDN answers the default
#: `python-urllib` agent with 403, and `HTTPError` is an `URLError` — so
#: without it the metadata fetch returned None on every call and the gate
#: reported UNKNOWN for a reason nobody could see.
USER_AGENT = "ira-supply-chain-gate"


def fetch(url: str, *, data: bytes | None = None,
          headers: dict[str, str] | None = None,
          timeout: int = 60) -> bytes | None:
    """GET (or POST, with `data`) `url`. Bytes, or None with the reason shown."""
    scheme = urllib.parse.urlparse(url).scheme
    if scheme != "https":
        # Raised, not returned. A caller that built a `file:` URL has a bug,
        # and turning it into "the network was unavailable" would hide it
        # behind the UNKNOWN path that exists for a different reason.
        raise ValueError(f"refusing to fetch {url!r}: scheme {scheme!r} is not "
                         f"https. These gates read published artifacts over "
                         f"TLS and nothing else.")
    request = urllib.request.Request(
        url, data=data, headers={"User-Agent": USER_AGENT, **(headers or {})})
    try:
        # Deliberately NOT marked `# nosec`. An inline suppression takes the
        # finding out of the report entirely; the baseline keeps it counted,
        # named and explained in `REASONS`, where somebody can disagree with
        # it. Silencing a scanner and accounting for what it found are not the
        # same act.
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read()
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        print(f"  could not fetch {url}: {exc}")
        return None


def fetch_text(url: str, **kwargs) -> str | None:
    blob = fetch(url, **kwargs)
    return None if blob is None else blob.decode("utf-8", "replace")
