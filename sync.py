from argparse import ArgumentParser, ArgumentTypeError
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Union

import itertools
import json
import os
import time
import uuid

import jsonschema
import requests
import schedule

from semver.version import Version


class Immich:
    def __init__(self, immich_url: str, api_key: str) -> None:
        self.immich_url = immich_url.rstrip("/")
        self.session = requests.Session()
        self.session.headers.update({
            "Content-Type": "application/json",
            "Accept": "application/json",
            "x-api-key": api_key,
        })

    def whoami(self):
        return self._get("/api/users/me")

    def version(self):
        return self._get("/api/server/version")

    def get_people(self) -> Iterable[Dict]:
        """Fetch all (non-hidden) people, transparently paginating."""
        page = 1
        while True:
            # requests serializes Python bools as "True"/"False" in query strings, but Immich's
            # query-param validation requires the lowercase literals "true"/"false"
            result = self._get("/api/people", params={"size": 1000, "withHidden": "false", "page": page})

            for person in result.get("people", []):
                yield person

            if not result.get("hasNextPage"):
                break

            page += 1

    def get_tags(self):
        return self._get("/api/tags")

    def get_albums(self):
        return self._get("/api/albums")

    def create_album(self, name: str, description: str = None):
        # me = self.whoami()

        album_params = {
            "albumName": name,
            # "albumUsers": [{
            #     "userId": me["id"],
            #     "role": "editor",
            # }]
        }

        if description:
            album_params["description"] = description

        return self._post("/api/albums", album_params)

    def delete_assets_from_album(self, album_id: str, assets_ids: List[str]):
        delete_params = {"ids": assets_ids}

        return self._delete(f"/api/albums/{album_id}/assets", delete_params)

    def add_assets_to_album(self, album_id: str, assets_ids: List[str]):
        add_params = {"ids": assets_ids}

        return self._put(f"/api/albums/{album_id}/assets", add_params)

    def search_metadata_assets(self, search_filter: Dict) -> Iterable[Dict]:
        """Search for assets matching a structured filter, transparently paginating via cursor."""
        cursor = None
        while True:
            search_params = {
                "filter": search_filter,
                "size": 1000,
                "withExif": True,
                "withPeople": True,
            }

            if cursor:
                search_params["cursor"] = cursor

            search_result = self._post("/api/search/metadata", search_params)
            assets_result = search_result.get("assets", {})

            for item in assets_result.get("items", []):
                yield item

            cursor = assets_result.get("nextCursor")
            if not cursor:
                break

    def search_smart_assets(self, search_filter: Dict, query: str, size: int = 1000) -> List[Dict]:
        """Perform a natural-language smart search, scoped by a structured filter.

        Smart search has no cursor-based pagination in Immich 3.2, so results are capped at
        `size` (at most 1000).
        """
        search_params = {
            "filter": search_filter,
            "query": query,
            "size": size,
            "withExif": True,
        }

        search_result = self._post("/api/search/smart", search_params)
        items = search_result.get("assets", {}).get("items", [])

        if len(items) >= size:
            print(
                f"Warning: smart_query {query!r} returned {len(items)} results, at or above the "
                f"limit of {size}. Results may be truncated since smart search cannot be paginated."
            )

        return items

    def _get(self, path, params: Optional[Dict] = None):
        return self._api("GET", path, params=params)

    def _put(self, path, json_body: Dict):
        return self._api("PUT", path, json_body=json_body)

    def _post(self, path, json_body: Dict):
        return self._api("POST", path, json_body=json_body)

    def _delete(self, path, json_body: Dict):
        return self._api("DELETE", path, json_body=json_body)

    def _api(self, verb: str, path: str, json_body: Optional[Dict] = None, params: Optional[Dict] = None):
        url = f"{self.immich_url}/{path.lstrip('/')}"

        response = self.session.request(verb, url, json=json_body, params=params, timeout=60)
        if response.status_code >= 400:
            print(url)
            print(response.text)

        response.raise_for_status()

        return response.json()


def create_album_if_not_exists(immich: Immich, album_name: str) -> str:
    albums = immich.get_albums()
    album_names = {album["albumName"]: album for album in albums}

    if album_name in album_names.keys():
        return album_names[album_name]

    album = immich.create_album(album_name)

    return album


def read_json(config_path: Union[Path, str]) -> Any:
    with open(config_path) as f:
        return json.load(f)


def build_search_filter(
    country: str = None,
    state: str = None,
    city: str = None,
    path: str = None,
    before: datetime = None,
    after: datetime = None,
    favorite: bool = None,
    person_ids: List[str] = None,
    tag_ids: List[str] = None,
    album_ids: List[str] = None,
) -> Dict:
    """Build an Immich `SearchFilter` object out of the tool's internal query representation.

    Only non-deprecated `SearchFilter` fields are used, per Immich >= 3.2. Assets are always
    scoped to the visible timeline (i.e. not archived/hidden), matching this tool's original intent.
    """
    search_filter: Dict[str, Any] = {"visibility": {"eq": "timeline"}}

    if country:
        search_filter["country"] = {"eq": country}
    if state:
        search_filter["state"] = {"eq": state}
    if city:
        search_filter["city"] = {"eq": city}
    if path:
        search_filter["originalPath"] = {"like": f"%{path}%"}

    taken_at = {}
    if after:
        taken_at["gte"] = after.isoformat()  # 2025-01-31T00:00:00
    if before:
        taken_at["lt"] = before.isoformat()  # 2025-01-31T23:59:59.999
    if taken_at:
        search_filter["takenAt"] = taken_at

    if favorite is not None:
        search_filter["isFavorite"] = {"eq": favorite}
    if person_ids:
        search_filter["personIds"] = {"all": person_ids}
    if tag_ids:
        search_filter["tagIds"] = {"all": tag_ids}
    if album_ids:
        search_filter["albumIds"] = {"all": album_ids}

    return search_filter


def run_search_query(immich: Immich, subquery: Dict) -> Iterable[Dict]:
    """Execute a single fanned-out subquery, routing to smart search when requested."""
    subquery = dict(subquery)
    smart_query = subquery.pop("smart_query", None)
    smart_query_limit = subquery.pop("smart_query_limit", None) or 1000

    search_filter = build_search_filter(**subquery)

    if smart_query:
        return immich.search_smart_assets(search_filter, smart_query, size=smart_query_limit)

    return list(immich.search_metadata_assets(search_filter))


def normalize_query_people(query: Dict, people_mapping: Dict[str, str]):
    if "people" not in query:
        return

    people = query["people"]
    if not isinstance(people, list):
        people = [people]

    person_ids = [
        person
        if is_valid_uuid(person) else people_mapping.get(person, None)
        for person in people
    ]

    if None in person_ids:
        invalid_people_names = [
            people[idx] for idx, name_or_id in enumerate(person_ids) if not name_or_id
        ]
        raise ValueError(f"The following names do not exist in Immich: {invalid_people_names}")

    query["person_ids"] = person_ids
    query.pop("people", None)


def normalize_query_tags(query: Dict, tag_mapping: Dict[str, str]):
    if "tags" not in query:
        return

    tags = query["tags"]
    if not isinstance(tags, list):
        tags = [tags]

    tag_ids = [
        tag
        if is_valid_uuid(tag) else tag_mapping.get(tag, None)
        for tag in tags
    ]

    if None in tag_ids:
        invalid_tags = [
            tags[idx] for idx, value_or_id in enumerate(tag_ids) if not value_or_id
        ]
        raise ValueError(f"The following tags do not exist in Immich: {invalid_tags}")

    query["tag_ids"] = tag_ids
    query.pop("tags", None)


def normalize_query_any_people(query: Dict, people_mapping: Dict[str, str]):
    if "any_people" not in query:
        return

    if "people" in query:
        raise ValueError("Cannot use 'people' (AND logic) and 'any_people' (OR logic) simultaneously in the same query block.")

    any_people = query["any_people"]
    if not isinstance(any_people, list):
        any_people = [any_people]

    any_person_ids = [
        person
        if is_valid_uuid(person) else people_mapping.get(person, None)
        for person in any_people
    ]

    if None in any_person_ids:
        invalid_people_names = [
            any_people[idx] for idx, name_or_id in enumerate(any_person_ids) if not name_or_id
        ]
        raise ValueError(f"The following names in 'any_people' do not exist in Immich: {invalid_people_names}")

    query["any_person_ids"] = any_person_ids
    query.pop("any_people", None)


def config_query_to_search_queries(query: Dict) -> Iterable[Dict]:
    # work on a copy so we never mutate the caller's config dict
    query = dict(query)

    # use 'None' as default to simplify the product operation below
    query_countries = query.pop("country", [None])
    if isinstance(query_countries, str):
        query_countries = [query_countries]
    elif not isinstance(query_countries, list):
        raise ValueError("'country' has to be either a string or a list of strings")

    query_timespans = query.pop("timespan", [])
    if isinstance(query_timespans, dict):
        query_timespans = [query_timespans]
    elif not isinstance(query_timespans, list):
        raise ValueError("'timespan' has to be either a dict or a list of dicts")

    query_timespans = [
        {
            "before": datetime.strptime(q["end"], "%Y-%m-%d") + timedelta(hours=24),
            "after": datetime.strptime(q["start"], "%Y-%m-%d")
        }
        for q in query_timespans
    ]

    if not query_timespans:
        query_timespans.append({"before": None, "after": None})

    any_person_ids = query.pop("any_person_ids", [None])

    # for r in itertools.product(a, b): print r[0] + r[1]
    for p in itertools.product(query_countries, query_timespans, any_person_ids):
        subquery = {
            "country": p[0],
            # unpack 'before' and 'after'
            **p[1],
            # unpack all other options, e.g. 'favorite'
            **query,
        }

        if p[2] is not None:
            subquery["person_ids"] = [p[2]]

        yield subquery


def is_valid_uuid(value: str) -> bool:
    try:
        return bool(uuid.UUID(value))
    except ValueError:
        return False


def valid_input_file_arg(arg: Union[Path, str]) -> Path:
    path = Path(arg).resolve()

    if not path.exists():
        raise ArgumentTypeError(f"Path does not exist: {arg}")
    if not path.is_file():
        raise ArgumentTypeError(f"Path is not a file: {arg}")

    return path


def sync_albums(args):
    # read the config and validate it against the schema
    configs = read_json(args.config_file)
    schema = read_json(Path(__file__).parent / "schema.json")
    jsonschema.validate(instance=configs, schema=schema)

    # create the api
    immich = Immich(args.immich_url, args.immich_api_key)

    # print version
    version_info = immich.version()
    immich_version = Version(version_info["major"], version_info["minor"], version_info["patch"])
    print(f"Immich version: {immich_version}")

    min_supported_version = Version(3, 2, 0)
    assert immich_version >= min_supported_version, f"Minimum supported version is {min_supported_version}"

    # prefetch all people to allow matching by name
    people_name_to_id = dict((p["name"], p["id"]) for p in immich.get_people())

    # prefetch all tags to allow matching by name
    tags = immich.get_tags()
    tag_value_to_id = dict((t["value"], t["id"]) for t in tags)

    for config in configs:
        album_name = config["name"]
        print(f"Processing album {album_name}")

        query = config["query"]
        normalize_query_people(query, people_name_to_id)
        normalize_query_tags(query, tag_value_to_id)
        normalize_query_any_people(query, people_name_to_id)

        people_strict_mode = query.pop("people_strict_mode", False)
        person_ids = query.get("person_ids", None)

        if query.get("smart_query") and people_strict_mode:
            raise ValueError(
                "Cannot use 'smart_query' together with 'people_strict_mode': "
                "smart search does not return face data."
            )

        # split the query into multiple subqueries depending on whether there are multiple
        # countries or timespans
        search_queries = list(config_query_to_search_queries(query))
        print(f"Album search queries: {search_queries}")

        search_results = [run_search_query(immich, subquery) for subquery in search_queries]
        search_results = list(itertools.chain(*search_results))

        if people_strict_mode and person_ids:
            search_results = [
                result for result in search_results
                if len(result.get("people") or []) == len(person_ids)
            ]

        # aggregate the asset ids from all search queries
        search_assets_ids = [asset["id"] for asset in search_results]

        # create the target album or find it amongst the other albums
        album_without_assets = create_album_if_not_exists(immich, album_name)

        # fetch the album's current assets using the search API
        album_filter = build_search_filter(album_ids=[album_without_assets["id"]])
        album_asset_results = list(immich.search_metadata_assets(album_filter))
        album_assets_ids = [asset["id"] for asset in album_asset_results]

        # calculate assets missing from the album and assets which should be removed from it
        album_missing_assets_ids = list(set(search_assets_ids) - set(album_assets_ids))
        album_extra_assets_ids = list(set(album_assets_ids) - set(search_assets_ids))

        print(f"Missing assets: {len(album_missing_assets_ids)}")
        print(f"Extra assets: {len(album_extra_assets_ids)}")

        if album_extra_assets_ids:
            immich.delete_assets_from_album(album_without_assets["id"], album_extra_assets_ids)

        if album_missing_assets_ids:
            immich.add_assets_to_album(album_without_assets["id"], album_missing_assets_ids)

        print("Done")


def parse_args():
    parser = ArgumentParser(description="Update dynamic albums")
    parser.add_argument(
        "--immich-url",
        default=os.environ.get("IMMICH_URL", "http://localhost:2283"),
    )
    parser.add_argument(
        "--immich-api-key",
        default=os.environ.get("IMMICH_API_KEY"),
    )
    parser.add_argument(
        "--config-file",
        type=valid_input_file_arg,
        default=os.environ.get("CONFIG_FILE"),
    )
    parser.add_argument(
        "--schedule-interval",
        type=int,
        default=os.environ.get("SCHEDULE_INTERVAL", 0),
        help="Schedule interval in minutes",
    )
    parser.add_argument(
        "--start-delay",
        type=int,
        default=os.environ.get("START_DELAY", 0),
        help="Delay the initial albums update (in seconds)",
    )

    return parser.parse_args()


def main():
    args = parse_args()

    assert args.immich_api_key, "immich-api-key is required"
    assert args.config_file, "config-file is required"

    # delay the initial start (e.g. to give time for the immich container to start) ...
    time.sleep(int(args.start_delay))

    # ... then run the sync process ...
    sync_albums(args)

    # ... and then schedule a job to continuously run (optionally)
    interval_in_minutes = args.schedule_interval

    if interval_in_minutes > 0:
        print(f"Scheduling the job to run every {interval_in_minutes} minutes")
        schedule.every(interval_in_minutes).minutes.do(sync_albums, args=args)

        while True:
            schedule.run_pending()
            time.sleep(60)


if __name__ == "__main__":
    main()
