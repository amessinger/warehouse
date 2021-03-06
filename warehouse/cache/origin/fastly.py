# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import urllib.parse

import requests

from zope.interface import implementer

from warehouse import celery
from warehouse.cache.origin.interfaces import IOriginCache


class UnsuccessfulPurge(Exception):
    pass


@celery.task(bind=True, ignore_result=True, acks_late=True)
def purge_key(task, request, key):
    cacher = request.find_service(IOriginCache)
    try:
        cacher.purge_key(key)
    except (requests.ConnectionError, requests.HTTPError, requests.Timeout,
            UnsuccessfulPurge) as exc:
        raise task.retry(exc=exc)


@implementer(IOriginCache)
class FastlyCache:

    _api_domain = "https://api.fastly.com"

    def __init__(self, *, api_key, service_id):
        self.api_key = api_key
        self.service_id = service_id

    @classmethod
    def create_service(cls, context, request):
        return cls(
            api_key=request.registry.settings["origin_cache.api_key"],
            service_id=request.registry.settings["origin_cache.service_id"],
        )

    def cache(self, keys, request, response, *, seconds=None,
              stale_while_revalidate=None, stale_if_error=None):
        response.headers["Surrogate-Key"] = " ".join(keys)

        values = []

        if seconds is not None:
            values.append("max-age={}".format(seconds))

        if stale_while_revalidate is not None:
            values.append(
                "stale-while-revalidate={}".format(stale_while_revalidate)
            )

        if stale_if_error is not None:
            values.append("stale-if-error={}".format(stale_if_error))

        if values:
            response.headers["Surrogate-Control"] = ", ".join(values)

    def purge(self, keys):
        for key in keys:
            purge_key.delay(key)

    def purge_key(self, key):
        path = "/service/{service_id}/purge/{key}".format(
            service_id=self.service_id,
            key=key,
        )
        url = urllib.parse.urljoin(self._api_domain, path)
        headers = {
            "Accept": "application/json",
            "Fastly-Key": self.api_key,
            "Fastly-Soft-Purge": "1",
        }

        resp = requests.post(url, headers=headers)
        resp.raise_for_status()

        if resp.json().get("status") != "ok":
            raise UnsuccessfulPurge(
                "Could not successfully purge {!r}".format(key)
            )

    def esi_include(self, request, path, *, cookies=False):
        @request.add_response_callback
        def _esi_vary(request, response):
            if cookies:
                response.headers["Warehouse-ESI-Vary"] = "Cookie"
            response.headers["Warehouse-ESI-Enable"] = "1"

        if cookies:
            path += "?esi-cookies=1"

        return '<esi:include src="{}" />'.format(path)
