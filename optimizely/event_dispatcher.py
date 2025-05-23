# Copyright 2016, 2022, Optimizely
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

import json
import logging
from sys import version_info

import requests
from requests import exceptions as request_exception
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from . import event_builder
from .helpers.enums import HTTPVerbs, EventDispatchConfig

if version_info < (3, 8):
    from typing_extensions import Protocol
else:
    from typing import Protocol  # type: ignore


class CustomEventDispatcher(Protocol):
    """Interface for a custom event dispatcher and required method `dispatch_event`. """

    def dispatch_event(self, event: event_builder.Event) -> None:
        ...


class NoOpEventDispatcher:
    """Event dispatcher that doesn't send any events to Optimizely's servers."""

    def dispatch_event(self, event: event_builder.Event) -> None:
        """No-op implementation that silently discards events.

        Args:
            event: Event object that would normally be sent to Optimizely's servers.
        """
        pass


class EventDispatcher:

    @staticmethod
    def dispatch_event(event: event_builder.Event) -> None:
        """ Dispatch the event being represented by the Event object.

    Args:
      event: Object holding information about the request to be dispatched to the Optimizely backend.
    """
        try:
            session = requests.Session()

            retries = Retry(total=EventDispatchConfig.RETRIES,
                            backoff_factor=0.1,
                            status_forcelist=[500, 502, 503, 504])
            adapter = HTTPAdapter(max_retries=retries)

            session.mount('http://', adapter)
            session.mount("https://", adapter)

            if event.http_verb == HTTPVerbs.GET:
                session.get(event.url, params=event.params,
                            timeout=EventDispatchConfig.REQUEST_TIMEOUT).raise_for_status()
            elif event.http_verb == HTTPVerbs.POST:
                session.post(
                    event.url, data=json.dumps(event.params), headers=event.headers,
                    timeout=EventDispatchConfig.REQUEST_TIMEOUT,
                ).raise_for_status()

        except request_exception.RequestException as error:
            logging.error(f'Dispatch event failed. Error: {error}')
