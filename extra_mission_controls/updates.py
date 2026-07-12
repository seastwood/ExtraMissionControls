"""Check GitHub for a newer release than the running version."""

import json

from Foundation import NSURL, NSURLRequest, NSURLSession, NSUserDefaults
from PyObjCTools import AppHelper

from . import __version__

_LATEST_URL = ("https://api.github.com/repos/seastwood/"
               "ExtraMissionControls/releases/latest")
_RELEASES_PAGE = "https://github.com/seastwood/ExtraMissionControls/releases"
# NSURLRequestReloadIgnoringLocalCacheData: a cached "latest release" response
# would defeat the point of re-checking.
_IGNORE_CACHE = 1

_IGNORE_KEY = "EMCIgnoredUpdateVersion"


def ignored_version():
    """The release version the user chose to skip, or None. A release newer
    than the ignored one still notifies."""
    return NSUserDefaults.standardUserDefaults().stringForKey_(_IGNORE_KEY)


def set_ignored_version(version):
    NSUserDefaults.standardUserDefaults().setObject_forKey_(
        version, _IGNORE_KEY)


def _parse(tag):
    """'EMC_0.9.2' / 'v0.9.2' / '0.9.2' -> (0, 9, 2); None if unparseable."""
    tag = (tag or "").strip()
    for prefix in ("EMC_", "v", "V"):
        if tag.startswith(prefix):
            tag = tag[len(prefix):]
    try:
        return tuple(int(part) for part in tag.split("."))
    except ValueError:
        return None


def check_async(on_update):
    """Fetch the latest release in the background; on the main thread, call
    on_update(version_string, release_url) only if it is newer than the
    running version. Network or parse failures are silently ignored — a
    missed check just means no badge until the next one.

    NSURLSession (not urllib) so TLS goes through the system trust store;
    the py2app bundle's Python has no CA bundle of its own.
    """
    request = NSURLRequest.requestWithURL_cachePolicy_timeoutInterval_(
        NSURL.URLWithString_(_LATEST_URL), _IGNORE_CACHE, 15.0)

    def handler(data, response, error):
        if error is not None or data is None:
            return
        try:
            info = json.loads(bytes(data))
        except ValueError:
            return
        if not isinstance(info, dict):
            return
        latest = _parse(info.get("tag_name"))
        current = _parse(__version__)
        if latest and current and latest > current:
            version = ".".join(str(part) for part in latest)
            url = info.get("html_url") or _RELEASES_PAGE
            AppHelper.callAfter(on_update, version, url)

    task = NSURLSession.sharedSession().dataTaskWithRequest_completionHandler_(
        request, handler)
    task.resume()
