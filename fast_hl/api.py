import json
import logging
from json import JSONDecodeError

# import requests
import aiohttp
import simdjson

from fast_hl.utils.constants import MAINNET_API_URL
from fast_hl.utils.error import ClientError, ServerError
from fast_hl.utils.types import Any


class API:

    async def __new__(cls, *a, **kw):
        instance = super().__new__(cls)
        await instance.__init__(*a, **kw)
        return instance

    async def __init__(
        self,
        base_url=None,
    ):
        self.base_url = MAINNET_API_URL
        # self.session = requests.Session()
        # self.session.headers.update(
        #     {
        #         "Content-Type": "application/json",
        #     }
        # )

        self.client = aiohttp.ClientSession(self.base_url)

        if base_url is not None:
            self.base_url = base_url

        self._logger = logging.getLogger(__name__)
        return

    async def post(self, url_path: str, payload: Any = None) -> Any:
        if payload is None:
            payload = {}
        url = self.base_url + url_path

        req = await self.client.post(url_path, data=simdjson.dumps(payload), headers={
            "Content-Type": "application/json",
        })

        self._handle_exception(req)

        response = simdjson.loads(await req.text()) # type: ignore
    
        try:
            return response
        except ValueError:
            return {"error": f"Could not parse JSON: {response}"}

    def _handle_exception(self, response):
        status_code = response.status
        if status_code < 400:
            return
        if 400 <= status_code < 500:
            try:
                err = response.json()
            except JSONDecodeError:
                raise ClientError(status_code, None, response.text, None, response.headers)
            error_data = None
            if "data" in err:
                error_data = err["data"]
            raise ClientError(status_code, err["code"], err["msg"], response.headers, error_data)
        raise ServerError(status_code, response.text)
