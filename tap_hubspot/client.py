"""REST client handling, including HubSpotStream base class."""

import gzip
import json
import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import IO, Any, Dict, Iterable, Optional
from uuid import uuid4

import requests
from dateutil.parser import parse as parse_datetime
from requests import Response
from singer_sdk.authenticators import BearerTokenAuthenticator
from singer_sdk.helpers._batch import BaseBatchFileEncoding, BatchConfig
from singer_sdk.helpers.jsonpath import extract_jsonpath
from singer_sdk.pagination import BaseAPIPaginator
from singer_sdk.streams import RESTStream
from singer_sdk.streams.core import REPLICATION_INCREMENTAL

SCHEMAS_DIR = Path(__file__).parent / Path("./schemas")


class HubSpotStream(RESTStream):
    """HubSpot stream class."""

    _LOG_REQUEST_METRICS = False

    url_base = "https://api.hubapi.com"

    # Or override `parse_response`.
    records_jsonpath = "$.results[*]"
    # Or override `get_next_page_token`.
    next_page_token_jsonpath = "$.paging.next.after"

    # Override in subclass to fetch additional properties
    properties_object_type = None
    # Used to cache extra properties fetched
    extra_properties = None

    # Set if forcing non-search endpoint
    forced_get = False

    @property
    def batch_size(self) -> int:
        return self.config.get("batch_size", 1_000_000)

    @property
    def rest_method(self) -> str:
        """Returns REST method depending on sync method"""
        # Called by prepare request
        if not self.forced_get and self.replication_method == REPLICATION_INCREMENTAL:
            return "POST"
        else:
            return "GET"

    @property
    def is_sorted(self) -> bool:
        """Check if stream is sorted.

        When `True`, incremental streams will attempt to resume if unexpectedly
        interrupted.

        This setting enables additional checks which may trigger
        `InvalidStreamSortException` if records are found which are unsorted.

        Returns:
            `True` if stream is sorted. Defaults to `False`.
        """
        # Hubspot has a bug in contacts
        return (
            self.replication_method == REPLICATION_INCREMENTAL
            and self.name != "contacts"
        )

    @property
    def authenticator(self) -> BearerTokenAuthenticator:
        """Return a new authenticator object."""
        return BearerTokenAuthenticator.create_for_stream(
            self, self.config.get("hapikey")
        )

    @property
    def http_headers(self) -> dict:
        """Return the http headers needed."""
        headers = {}
        if "user_agent" in self.config:
            headers["User-Agent"] = self.config.get("user_agent")
        # If not using an authenticator, you may also provide inline auth headers:
        # headers["Private-Token"] = self.config.get("auth_token")
        return headers

    def get_new_paginator(self) -> BaseAPIPaginator:
        """Get a fresh paginator for this API endpoint.

        Returns:
            A paginator instance.
        """
        return HubspotJSONPathPaginator(
            self.next_page_token_jsonpath,
            forced_get=self.forced_get,
            replication_method=self.replication_method,
            test=self.config.get("test", False),
        )

    def get_url_params(
        self, context: Optional[dict], next_page_token: Optional[Any]
    ) -> Dict[str, Any]:
        """Return a dictionary of values to be used in URL parameterization."""
        if not self.forced_get and self.replication_method == REPLICATION_INCREMENTAL:
            # Handled in prepare_request_payload instead
            return {}

        params: dict = {
            # Hubspot sets a limit of most 100 per request. Default is 10
            "limit": self.config.get("limit", 100)
        }
        props_to_get = self.get_properties()
        if props_to_get:
            params["properties"] = props_to_get
        if next_page_token:
            params["after"] = next_page_token
        return params

    def prepare_request_payload(
        self, context: Optional[dict], next_page_token: Optional[Any]
    ) -> Optional[dict]:
        """Prepare the data payload for the REST API request.

        By default, no payload will be sent (return None).

        Developers may override this method if the API requires a custom payload along
        with the request. (This is generally not required for APIs which use the
        HTTP 'GET' method.)

        Args:
            context: Stream partition or context dictionary.
            next_page_token: Token, page number or any request argument to request the
                next page of data.

        Returns:
            Dictionary with the body to use for the request.
        """
        if self.forced_get or self.replication_method != REPLICATION_INCREMENTAL:
            return None

        # Datetime object
        starting_replication_value: datetime = self.get_starting_timestamp(context)
        # If no state exists, then fallback to config
        if not starting_replication_value:
            start_from = self.config.get("start_from", None)
            if start_from:
                try:
                    starting_replication_value = parse_datetime(start_from)
                except Exception:
                    logging.error(
                        f"Could not parse starting date: '{start_from}'",
                        file=sys.stderr,
                    )
                    pass

        body: dict = {
            "sorts": [
                {
                    # This is inside the properties object
                    "propertyName": self.replication_key,
                    "direction": "ASCENDING",
                }
            ],
            # Hubspot sets a limit of most 100 per request. Default is 10
            "limit": 100,
        }

        props_to_get = self.get_properties()
        if props_to_get:
            body["properties"] = props_to_get

        if next_page_token:
            body["after"] = next_page_token

        if starting_replication_value:
            # Only filter in case we have a value to filter on
            body["filterGroups"] = [
                {
                    "filters": [
                        {
                            "propertyName": self.replication_key,
                            "operator": "GTE",
                            # It's never specified anywhere, but Hubspot API accepts
                            # timestamps in milliseconds
                            "value": int(starting_replication_value.timestamp() * 1000),
                        }
                    ]
                }
            ]

        return body

    def get_properties(self) -> Iterable[str]:
        """Override to return a list of properties to fetch for objects"""
        if self.extra_properties is not None:
            return self.extra_properties

        if not self.properties_object_type:
            self.extra_properties = []
            return self.extra_properties

        request = self.build_prepared_request(
            method="GET",
            url="".join(
                [self.url_base, f"/crm/v3/properties/{self.properties_object_type}"]
            ),
            headers=self.http_headers,
        )

        session = self._requests_session or requests.Session()

        r = session.send(request)

        if r.status_code != 200:
            raise RuntimeError(f"Could not fetch properties: {r.status_code}, {r.text}")

        self.extra_properties = []
        for p in extract_jsonpath("$.results[*]", input=r.json()):
            self.extra_properties.append(p["name"])
        return self.extra_properties

    def parse_response(self, response: requests.Response) -> Iterable[dict]:
        """Parse the response and return an iterator of result rows."""
        yield from extract_jsonpath(self.records_jsonpath, input=response.json())

    def post_process(self, row: dict, context: Optional[dict]) -> dict:
        """As needed, append or transform raw data to match expected structure."""
        # Need to copy the replication key to top level so that meltano can read it
        if self.replication_key:
            row[self.replication_key] = self.get_replication_key_value(row)
        # Convert properties and associations back into JSON
        if "properties" in row:
            jsonprops = json.dumps(row.get("properties"))
            row["properties"] = jsonprops
        if "associations" in row:
            jsonassoc = json.dumps(row.get("associations"))
            row["associations"] = jsonassoc
        return row

    def get_replication_key_value(self, row: dict) -> Optional[datetime]:
        """Reads the replication value from a record. Default implementation assumes
        it lives inside of properties object"""
        if not self.replication_key or "properties" not in row:
            return None

        # String like 2022-04-13T07:41:30.007Z
        return parse_datetime(row["properties"][self.replication_key])

    def get_batches(
        self,
        batch_config: BatchConfig,
        context: Optional[dict] = None,
    ) -> Iterable[tuple[BaseBatchFileEncoding, list[str]]]:
        """Batch generator function.

        Developers are encouraged to override this method to customize batching
        behavior for databases, bulk APIs, etc.

        Args:
            batch_config: Batch config for this stream.
            context: Stream partition or context dictionary.

        Yields:
            A tuple of (encoding, manifest) for each batch.
        """
        sync_id = f"{self.tap_name}--{self.name}-{uuid4()}"
        prefix = batch_config.storage.prefix or ""

        i = 1
        chunk_size = 0
        filename: Optional[str] = None
        f: Optional[IO] = None
        gz: Optional[gzip.GzipFile] = None

        with batch_config.storage.fs() as fs:
            for record in self._sync_records(context, write_messages=False):
                if chunk_size >= self.batch_size:
                    gz.close()
                    gz = None
                    f.close()
                    f = None
                    file_url = fs.geturl(filename)
                    yield batch_config.encoding, [file_url]

                    filename = None

                    i += 1
                    chunk_size = 0

                if filename is None:
                    filename = f"{prefix}{sync_id}-{i}.json.gz"
                    f = fs.open(filename, "wb")
                    gz = gzip.GzipFile(fileobj=f, mode="wb")

                gz.write((json.dumps(record, default=str) + "\n").encode())
                chunk_size += 1

            if chunk_size > 0:
                gz.close()
                f.close()
                file_url = fs.geturl(filename)
                yield batch_config.encoding, [file_url]


class HubspotJSONPathPaginator(BaseAPIPaginator[Optional[str]]):
    """Paginator class for APIs returning a pagination token in the response body."""

    def __init__(
        self,
        jsonpath: str,
        forced_get: bool,
        replication_method: str,
        test: Any,
        *args: Any,
        **kwargs: Any,
    ) -> None:
        """Create a new paginator.

        Args:
            jsonpath: A JSONPath expression.
            args: Paginator positional arguments for base class.
            kwargs: Paginator keyword arguments for base class.
        """
        super().__init__(None, *args, **kwargs)
        self._jsonpath = jsonpath
        self.forced_get = forced_get
        self.replication_method = replication_method
        self.test = test

    def get_next(self, response: Response) -> Optional[str]:
        """Get the next page token.

        Args:
            response: API response object.

        Returns:
            The next page token.
        """
        if self.test:
            return None

        all_matches = extract_jsonpath(self._jsonpath, response.json())
        next_page_token = next(iter(all_matches), None)

        try:
            # Here a quirk: If more than 10 000 results are in the query,
            # then HubSpot will return error 400 when you exceed 10 000.
            # So stop early. Run another sync to pickup from where
            # you left off.
            if (
                not self.forced_get
                and self.replication_method == REPLICATION_INCREMENTAL
                and int(next_page_token) + 100 >= 10000
            ):
                next_page_token = None
        except Exception:
            # Not an int, so can't do anything
            pass

        return next_page_token
