import json
import logging
from dataclasses import dataclass, field
from typing import Callable, TypeVar, cast
import time

import requests
import websocket

from .models import *


@dataclass
class ClientConfig:
    league: str
    poesessid: str  # could be replaced with OAuth
    url: str = "https://www.pathofexile.com/api/trade/"
    default_headers: dict[str, str] = field(
        default_factory=lambda: {
            # have to fake the User-Agent to not get a 403
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:109.0) Gecko/20100101 Firefox/111.0",
            "Accept": "*/*",
            "Content-Type": "application/json",
        }
    )
    log_level: int = logging.WARNING


@dataclass
class SearchConfig:
    item_name: str
    item_type: str
    online_opt: OnlineStatus = "online"
    price_sort: SortPrice = "asc"
    stat_filters: list[QueryStat] = field(
        default_factory=lambda: [{"type": "and", "filters": [], "disabled": False}]
    )
    query_filters: QueryFilters = field(default_factory=lambda: {})
    live: bool = False
    live_on_item_callback: Callable[[FetchResponse], None] | None = None
    page_limit: int = 1


class TradeClient:
    _config: ClientConfig
    _sess: requests.Session | None = None
    _logger: logging.Logger

    def __init__(self, cfg: ClientConfig) -> None:
        self._config = cfg

        logging.basicConfig(level=cfg.log_level)
        self._logger = logging.getLogger("TradeClient")

    @property
    def config(self):
        return self._config

    def _build_headers(self, extras: dict[str, str] = {}):
        h = self.config.default_headers
        h.update({"Cookie": f"POESESSID={self.config.poesessid}", **extras})
        return h

    def _build_search_url(self):
        return self.config.url + "search/" + self.config.league

    def _build_fetch_url(self, search_results: list[str]):
        return self.config.url + "fetch/" + ",".join(search_results)

    def _build_livesearch_url(self, query_id: str):
        return (
            self.config.url.replace("https", "wss")
            + f"live/{self.config.league}/{query_id}"
        )

    def _build_whisper_url(self):
        return self.config.url + "whisper"
    
    def _build_trade_id_url(self, id: str) -> str:
        return f"{self.config.url}search/{self.config.league}/{id}"

    def _request(self, req: requests.Request, raise_error: bool = True):
        if not self._sess:
            self._sess = requests.Session()

        self._logger.debug(f"Request to send\n{req.__dict__}\n")
        res = self._sess.send(req.prepare(), allow_redirects=True)
        self._logger.debug(f"Full response {res.__dict__}")
        if raise_error:
            res.raise_for_status()
        return res

    def _search(self, req: TradeRequest) -> SearchResponse:
        return self._request(
            requests.Request(
                method="POST",
                url=self._build_search_url(),
                headers=self._build_headers(),
                data=json.dumps(req),
            )
        ).json()

    def _fetch(self, built_url: str, query_id: str) -> FetchResponse:
        return self._request(
            requests.Request(
                method="GET",
                url=built_url,
                params={"query": query_id},
                headers=self._build_headers(),
            )
        ).json()

    def _whisper(self, whisper_token: str) -> WhisperResponse:
        return self._request(
            requests.Request(
                method="POST",
                url=self._build_whisper_url(),
                data=json.dumps({"token": whisper_token}),
                headers=self._build_headers({"X-Requested-With": "XMLHttpRequest"}),
            ),
            raise_error=False,
        ).json()

    def _build_trade_request(self, cfg: SearchConfig) -> TradeRequest:
        req = {
            "query": {
                "status": {"option": cfg.online_opt},
                "name": cfg.item_name,
                "type": cfg.item_type,
                "stats": cfg.stat_filters,
                "filters": cfg.query_filters,
            },
            "sort": {"price": cfg.price_sort},
        }
        if cfg.item_name == "":
            del req['query']['name']
        if cfg.item_type == "":
            del req['query']['type']
        return req

    _TPage = TypeVar("_TPage")

    def _build_pages(
        self, all_results: list[_TPage], page_width=10
    ) -> list[list[_TPage]]:
        if len(all_results) <= page_width:
            return [all_results]

        pages = []
        page = []
        for r in all_results:
            if len(page) < page_width:
                page.append(r)
            else:
                pages.append(page)
                page = []
                page.append(r)
        return pages

    def _build_ws_message_handler(
        self,
        search_id: str,
        fetch_callback: Callable[[FetchResponse], None] | None,
    ):
        def on_message(ws, msg):
            self._logger.debug(f"Received msg {msg}")
            if "new" in msg:
                new_item: list[str] = json.loads(msg)["new"]
                fetch_res = self._fetch(self._build_fetch_url(new_item), search_id)
                if fetch_callback:
                    fetch_callback(fetch_res)

        return on_message

    def _normal_search(self, cfg: SearchConfig) -> FetchResponse:
        search_res = self._search(self._build_trade_request(cfg))
        # if more than 10 results have to paginate them
        paged_ids = self._build_pages(search_res["result"])
        # Warning, API will only return 10 pages at most even though the actual search found more than that
        page_size = min(cfg.page_limit, len(paged_ids))
        result: FetchResponse = {"result": []}
        for i in range(page_size):
            fetch_res = self._fetch(self._build_fetch_url(paged_ids[i]), search_res["id"])
            result["result"] += fetch_res["result"]
            # Rate limit the API calls to adhere with GGG rules
            # See https://www.pathofexile.com/developer/docs/index#ratelimits
            # TODO change the hardcoded sleep with reading the actual rules in response header
            time.sleep(2)
        return result

    def _live_search(self, cfg: SearchConfig):
        if self.config.log_level <= logging.DEBUG:
            websocket.enableTrace(True)

        # first have to get the search id
        search_id = self._search(self._build_trade_request(cfg))["id"]

        on_message = self._build_ws_message_handler(
            search_id, cfg.live_on_item_callback
        )

        wsapp = websocket.WebSocketApp(
            self._build_livesearch_url(search_id),
            on_open=lambda ws: print(f"Starting livesearch for {cfg.item_name}"),
            on_message=on_message,
            header=self._build_headers(),
        )
        wsapp.run_forever()

    def search(self, cfg: SearchConfig):
        if cfg.live:
            return self._live_search(cfg)
        return self._normal_search(cfg)

    def whisper(self, listing: ItemListing):
        r = self._whisper(listing["whisper_token"])
        if "error" in r:
            self._logger.warning(f"Whisper error: {r['error']['message']}")  # type: ignore
        return r

    def build_config(self, trade_id: str) -> SearchConfig:
        """ 
        Generate a SearchConfig object from a trade ID.
        Mainly this is a convenience method since manually creating search parameters from code can be cumbersome.
        To get trade ID: 
        1. Go to https://www.pathofexile.com/trade
        2. Build your search filters like how you usually do it
        3. Click "Search"
        4. Copy the ID in the last part of search result url, should looks like this "Ab3LSL"
        """
        resp = self._request(
            requests.Request(
                method="GET",
                url=self._build_trade_id_url(trade_id),
                headers=self._build_headers(),
            )).json()
        query = resp['query']
        print(query)
        search_config = SearchConfig("", "")
        if 'name' in query:
            search_config.item_name = query['name']
        if 'type' in query:
            search_config.item_type = query['type']
        if 'stats' in query:
            search_config.stat_filters = query['stats']
        if 'filters' in query:
            search_config.query_filters = query['filters']
        if 'status' in query:
            search_config.online_opt = query['status']['option']
        return search_config