import calendar
import os

import newrelic.agent
import requests
from dateutil import parser
from django.conf import settings


def make_request(url, method='GET', headers=None, timeout=30, **kwargs):
    """A wrapper around requests to set defaults & call raise_for_status()."""
    headers = headers or {}
    headers['User-Agent'] = 'treeherder/{}'.format(settings.SITE_HOSTNAME)
    if os.environ.get("GITHUB_TOKEN") and url.find("api.github.com") > -1:
        headers["Authorization"] = "token {}".format(os.environ["GITHUB_TOKEN"])
    response = requests.request(method,
                                url,
                                headers=headers,
                                timeout=timeout,
                                **kwargs)
    if response.history:
        params = {
            'url': url,
            'redirects': len(response.history),
            'duration': sum(r.elapsed.total_seconds() for r in response.history)
        }
        newrelic.agent.record_custom_event('RedirectedRequest', params=params)

    response.raise_for_status()
    return response


def fetch_json(url, params=None, headers={}):
    headers["Accept"] = "application/json"
    response = make_request(url,
                            params=params,
                            headers=headers)
    return response.json()


def fetch_text(url):
    response = make_request(url)
    return response.text


def get_guid_root(guid):
    """Converts a job_guid with endtime suffix to normal job_guid"""
    if "_" in str(guid):
        return str(guid).split("_", 1)[0]
    return guid


def to_timestamp(datestr):
    """Converts a date string to a UTC timestamp"""
    return calendar.timegm(parser.parse(datestr).utctimetuple())
