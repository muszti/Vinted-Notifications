import db
import html
import time
import requests
from collections import OrderedDict
from pyVintedVN import Vinted, requester
from pyVintedVN.items.item import category_from_page
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse
from logger import get_logger

# Get logger for this module
logger = get_logger(__name__)

# Vinted stopped publishing listing times in September 2026, so "is this item new?"
# is answered purely by "have we recorded its id before?". That makes a wiped or
# freshly seeded database the only real flood risk, and this caps the blast radius.
MAX_NOTIFICATIONS_PER_RUN = 10

# A brand-new query announces everything it currently lists, paging through the
# results. Capped because every item becomes a Telegram message (flood limits) and
# every page is a Vinted request (429 risk); a query matching thousands of listings
# would otherwise hammer both.
FIRST_RUN_MAX_ITEMS = 300
# Pause between first-run pages so paging doesn't look like a burst to Vinted.
FIRST_RUN_PAGE_DELAY = 1

# Seller country lookups cost one API request each and a seller's country doesn't
# change, so remember them. Bounded (LRU) so a long-running process can't grow forever,
# and expiring so a seller who moves is eventually re-checked.
_COUNTRY_CACHE = OrderedDict()
_COUNTRY_CACHE_MAX = 5000
_COUNTRY_CACHE_TTL = 24 * 3600


def process_query(query, name=None, banwords=None):
    """
    Process a Vinted query URL by:
    1. Checking if the URL is a brand URL and converting it to standard format if needed
    2. Parsing the URL and extracting query parameters
    3. Ensuring the order flag is set to "newest_first"
    4. Removing time and search_id parameters
    5. Rebuilding the query string and URL
    6. Checking if the query already exists in the database
    7. Adding the query to the database if it doesn't exist

    Args:
        query (str): The Vinted query URL
        name (str, optional): A name for the query. If provided, it will be used as the query name.
        banwords (str, optional): Banwords for this query only, separated by "|||".

    Returns:
        tuple: (message, is_new_query)
            - message (str): Status message
            - is_new_query (bool): True if query was added, False if it already existed
    """
    # Check if the URL is a brand URL (format: url/brand/id-name)
    parsed_url = urlparse(query)
    path_parts = parsed_url.path.strip("/").split("/")

    if len(path_parts) >= 2 and path_parts[0] == "brand":
        # Extract the brand ID from the format "id-name"
        brand_id_with_name = path_parts[1]
        brand_id = brand_id_with_name.split("-")[0]

        # Create a new URL with the standard format
        new_path = "/catalog"
        new_query_params = {"brand_ids[]": [brand_id]}
        new_query_string = urlencode(new_query_params, doseq=True)

        # Rebuild the URL
        query = urlunparse(
            (parsed_url.scheme, parsed_url.netloc, new_path, "", new_query_string, "")
        )
        logger.info(f"Converted brand URL to standard format: {query}")

        # Parse the URL and extract the query parameters
        parsed_url = urlparse(query)

    query_params = parse_qs(parsed_url.query)

    # Ensure the order flag is set to newest_first
    query_params["order"] = ["newest_first"]
    # Remove time and search_id if provided
    query_params.pop("time", None)
    query_params.pop("search_id", None)
    query_params.pop("disabled_personalization", None)
    query_params.pop("page", None)

    # Rebuild the query string and the entire URL
    new_query = urlencode(query_params, doseq=True)
    processed_query = urlunparse(
        (
            parsed_url.scheme,
            parsed_url.netloc,
            parsed_url.path,
            parsed_url.params,
            new_query,
            parsed_url.fragment,
        )
    )

    # Some queries are made with filters only, so we need to check if the search_text is present
    if db.is_query_in_db(processed_query) is True:
        return "Query already exists.", False
    else:
        # add the query to the db
        db.add_query_to_db(processed_query, name, banwords)
        return "Query added.", True


def get_formatted_query_list():
    """
    Get a formatted list of all queries in the database.

    Returns:
        str: A formatted string with all queries, numbered
    """
    all_queries = db.get_queries() or []
    queries_keywords = []
    for query in all_queries:
        # Get the name, else the value of 'search_text', else the whole query URL
        query_name = (
            query[3] or parse_qs(urlparse(query[1]).query).get("search_text", [None])[0] or query[1]
        )
        queries_keywords.append(query_name)

    query_list = ("\n").join(
        [str(i + 1) + ". " + j for i, j in enumerate(queries_keywords)]
    )
    return query_list


def process_remove_query(number):
    """
    Process the removal of a query from the database.

    Args:
        number (str): The number of the query to remove or "all" to remove all queries

    Returns:
        tuple: (message, success)
            - message (str): Status message
            - success (bool): True if query was removed successfully
    """
    if number == "all":
        db.remove_all_queries_from_db()
        return "All queries removed.", True

    # Check if number is a valid digit
    if number.isdigit():
        # Remove the query from the database
        db.remove_query_from_db(number)
        return "Query removed.", True
    else:
        return "Invalid number.", False


def process_update_query(query_id, query, name, banwords=None):
    """
    Process the update of a query in the database.

    Args:
        query_id (int): The ID of the query to update
        query (str): The new Vinted query URL
        name (str, optional): A new name for the query. If provided, it will be used as the query name.
        banwords (str, optional): Banwords for this query only, separated by "|||".

    Returns:
        tuple: (message, success)
            - message (str): Status message
            - success (bool): True if query was updated successfully
    """
    # Parse the URL and extract the query parameters
    parsed_url = urlparse(query)
    query_params = parse_qs(parsed_url.query)

    # Ensure the order flag is set to newest_first
    query_params["order"] = ["newest_first"]
    # Remove time and search_id if provided
    query_params.pop("time", None)
    query_params.pop("search_id", None)
    query_params.pop("disabled_personalization", None)
    query_params.pop("page", None)

    # Rebuild the query string and the entire URL
    new_query = urlencode(query_params, doseq=True)
    processed_query = urlunparse(
        (
            parsed_url.scheme,
            parsed_url.netloc,
            parsed_url.path,
            parsed_url.params,
            new_query,
            parsed_url.fragment,
        )
    )

    # Update the query in the database
    if db.update_query_in_db(query_id, processed_query, name, banwords):
        return "Query updated.", True
    else:
        return "Failed to update query.", False


def process_add_country(country):
    """
    Process the addition of a country to the allowlist.

    Args:
        country (str): The country code to add

    Returns:
        tuple: (message, country_list)
            - message (str): Status message
            - country_list (list): Current list of allowed countries
    """
    # Format the country code (remove spaces)
    country = country.replace(" ", "")
    country_list = db.get_allowlist()

    # Validate the country code (check if it's 2 characters long)
    if len(country) != 2:
        return "Invalid country code", country_list

    # Check if the country is already in the allowlist
    # If country_list is 0, it means the allowlist is empty
    if country_list != 0 and country.upper() in country_list:
        return f'Country "{country.upper()}" already in allowlist.', country_list

    # Add the country to the allowlist
    db.add_to_allowlist(country.upper())
    return "Country added.", db.get_allowlist()


def process_remove_country(country):
    """
    Process the removal of a country from the allowlist.

    Args:
        country (str): The country code to remove

    Returns:
        tuple: (message, country_list)
            - message (str): Status message
            - country_list (list): Current list of allowed countries
    """
    # Format the country code (remove spaces)
    country = country.replace(" ", "")

    # Validate the country code (check if it's 2 characters long)
    if len(country) != 2:
        return "Invalid country code", db.get_allowlist()

    # Remove the country from the allowlist
    db.remove_from_allowlist(country.upper())
    return "Country removed.", db.get_allowlist()


def get_user_country(profile_id):
    """
    Get the country code for a Vinted user.

    Results are cached in memory (LRU, bounded, 24h expiry). Otherwise makes an API
    request to retrieve the user's country code, handling rate limiting by trying an
    alternative endpoint. Never raises: any failure yields "XX".

    Args:
        profile_id (str): The Vinted user's profile ID

    Returns:
        str: The user's country code (2-letter ISO code) or "XX" if it can't be determined.
            "XX" is never cached: it passes the allowlist, so remembering a transient
            failure would let that seller through for the whole TTL.
    """
    key = str(profile_id)
    cached = _COUNTRY_CACHE.get(key)
    if cached is not None:
        country, stored_at = cached
        if time.time() - stored_at < _COUNTRY_CACHE_TTL:
            _COUNTRY_CACHE.move_to_end(key)
            return country
        del _COUNTRY_CACHE[key]

    try:
        # Users are shared between all Vinted platforms, so we can use whatever locale we want
        url = f"https://www.vinted.fr/api/v2/users/{profile_id}?localize=false"
        response = requester.get(url)
        # That's a LOT of requests, so if we get a 429 we wait a bit before retrying once
        if response.status_code == 429:
            # In case of rate limit, we're switching the endpoint. This one is slower, but it doesn't RL as soon.
            # We're limiting the items per page to 1 to grab as little data as possible
            url = f"https://www.vinted.fr/api/v2/users/{profile_id}/items?page=1&per_page=1"
            response = requester.get(url)
            try:
                user_country = response.json()["items"][0]["user"]["country_iso_code"]
            except (KeyError, IndexError):
                logger.warning(
                    "Couldn't get the country due to too many requests. Returning default value."
                )
                return "XX"
        else:
            user_country = response.json()["user"]["country_iso_code"]
    except Exception as e:
        # A seller we can't look up must not abort the whole batch of items
        logger.warning(f"Couldn't get the country of user {profile_id}: {e}")
        return "XX"

    if not user_country or user_country == "XX":
        return "XX"
    _COUNTRY_CACHE[key] = (user_country, time.time())
    _COUNTRY_CACHE.move_to_end(key)
    while len(_COUNTRY_CACHE) > _COUNTRY_CACHE_MAX:
        _COUNTRY_CACHE.popitem(last=False)
    return user_country


def process_items(queue):
    """
    Process all queries from the database, search for items, and put them in the queue.
    Uses the global items_queue by default, but can accept a custom queue for backward compatibility.

    Args:
        queue (Queue, optional): The queue to put the items in. Defaults to the global items_queue.

    Returns:
        None
    """

    all_queries = db.get_queries() or []

    # Initialize Vinted
    vinted = Vinted()

    # Get the number of items per query from the database
    items_per_query = int(db.get_parameter("items_per_query"))

    # for each keyword we parse data
    for query in all_queries:
        # One failing query (bad URL, 429, network blip) must not starve the others
        try:
            # last_item (column 2) is None until a query has produced its first item
            is_first_run = query[2] is None
            if is_first_run:
                all_items = fetch_first_run_items(vinted, query[1], items_per_query)
            else:
                all_items = vinted.items.search(query[1], nbr_items=items_per_query)
            # Filter to only include new items. This should reduce the amount of db calls.
            data = [item for item in all_items if item.is_new_item()]
            queue.put((data, query[0]))
            logger.info(f"Scraped {len(data)} items for query: {query[1]}")
        except Exception as e:
            logger.error(f"Query {query[0]} failed: {e}", exc_info=True)
            continue


def fetch_first_run_items(vinted, query_url, items_per_query):
    """
    Fetch every item a brand-new query currently lists, paging until exhausted.

    Args:
        vinted (Vinted): The Vinted client
        query_url (str): The query URL
        items_per_query (int): Page size to request

    Returns:
        list: Unique items, newest first, at most FIRST_RUN_MAX_ITEMS. A failure on
            page 1 propagates (the query is retried next cycle); a failure on a later
            page keeps what was collected so far.
    """
    collected = []
    # Results are newest first, so listings added while we page shift later pages
    # and can repeat an item; dedupe by id.
    seen = set()
    page = 1
    while True:
        try:
            items = vinted.items.search(query_url, nbr_items=items_per_query, page=page)
        except Exception as e:
            if page == 1:
                raise
            logger.warning(
                f"Page {page} failed for query {query_url}: {e}. "
                f"Keeping the {len(collected)} item(s) fetched so far."
            )
            break
        if not items:
            break
        added = 0
        for item in items:
            if item.id not in seen:
                seen.add(item.id)
                collected.append(item)
                added += 1
        # A page of only known ids means the API is repeating itself; stop rather than loop forever.
        # Deliberately no "short page means last page" check: the server may clamp per_page.
        if added == 0:
            break
        if len(collected) >= FIRST_RUN_MAX_ITEMS:
            logger.warning(
                f"First run for query {query_url} hit the {FIRST_RUN_MAX_ITEMS} item cap; "
                f"ignoring the remaining listings."
            )
            collected = collected[:FIRST_RUN_MAX_ITEMS]
            break
        page += 1
        time.sleep(FIRST_RUN_PAGE_DELAY)
    logger.info(
        f"First run for query {query_url}: fetched {len(collected)} item(s) over {page} page(s)"
    )
    return collected


def get_query_display_name(query_id, query_row):
    """
    Get the name shown for a query in notifications: its name, else its search text, else its id.

    Args:
        query_id (int): The ID of the query
        query_row (tuple): The row from db.get_query_by_id, or None if the query no longer exists

    Returns:
        str: The name to show for the query
    """
    if query_row is None:
        return f"Query #{query_id}"
    _, query_url, _, query_name, _ = query_row
    if query_name:
        return query_name
    search_text = parse_qs(urlparse(query_url).query).get("search_text", [None])[0]
    return search_text or f"Query #{query_id}"


def clear_item_queue(items_queue, new_items_queue):
    """
    Process items from the items_queue.
    This function is scheduled to run frequently.
    """
    if not items_queue.empty():
        data, query_id = items_queue.get()
        query_row = db.get_query_by_id(query_id)
        query_name = get_query_display_name(query_id, query_row)
        # Global banwords apply to every query, the query's own list only to that query
        global_banwords = db.get_parameter("banwords")
        query_banwords = query_row[4] if query_row and query_row[4] else ""
        banwords_str = "|||".join(w for w in [global_banwords, query_banwords] if w)

        # Read the watermark once, before the loop. It doubles as the "has this query
        # ever produced anything?" flag, and the updates made below would otherwise
        # cut a first-run priming pass short right after the first item.
        last_query_timestamp = db.get_last_timestamp(query_id)
        is_first_run = last_query_timestamp is None
        if is_first_run:
            logger.info(
                f"First run for query {query_id}: {len(data)} item(s) currently listed, "
                f"the most recent {MAX_NOTIFICATIONS_PER_RUN} will be announced."
            )

        # Read once: the allowlist doesn't change mid-batch and this was a db hit per item
        allowlist = db.get_allowlist()

        to_notify = []
        for item in reversed(data):

            # The watermark is only meaningful when the API actually supplied a
            # listing time. Otherwise raw_timestamp is merely when we saw the item,
            # and comparing it against the watermark would discard every new item.
            if (
                item.has_real_timestamp
                and last_query_timestamp is not None
                and last_query_timestamp >= item.raw_timestamp
            ):
                continue
            # In case of multiple queries, we need to check if the item is already in the db
            if db.is_item_in_db_by_id(item.id) is True:
                # We update the timestamp
                db.update_last_timestamp(query_id, item.raw_timestamp)
                continue
            # If there's an allowlist and
            # If the user's country is not in the allowlist, we just update the timestamp
            if allowlist != 0 and (
                get_user_country(item.raw_data["user"]["id"])
            ) not in (allowlist + ["XX"]):
                db.update_last_timestamp(query_id, item.raw_timestamp)
                continue
            # Check if the item title contains any banwords
            if banwords_str and contains_banwords(item.title, banwords_str):
                # If it contains banwords, just update the timestamp and skip
                db.update_last_timestamp(query_id, item.raw_timestamp)
                continue

            # Being recorded is what stops an item coming back next run, so every
            # item that reaches this point is written to the db whether or not it
            # ends up being announced.
            to_notify.append(item)
            db.add_item_to_db(
                id=item.id,
                timestamp=item.raw_timestamp,
                price=item.price,
                title=item.title,
                photo_url=item.photo,
                query_id=query_id,
                currency=item.currency,
                condition=item.condition,
            )

        # data arrives newest first and is walked in reverse, so to_notify runs
        # oldest to newest. Trim from the front: a backlog is worth dropping, the
        # listings that just appeared are not. This applies to a first run too: the
        # first run fetches everything listed (FIRST_RUN_MAX_ITEMS across pages), but
        # only the most recent MAX_NOTIFICATIONS_PER_RUN are announced.
        if len(to_notify) > MAX_NOTIFICATIONS_PER_RUN:
            logger.warning(
                f"{len(to_notify)} unseen items for query {query_id}; notifying the "
                f"{MAX_NOTIFICATIONS_PER_RUN} most recent and recording the rest silently."
            )
            to_notify = to_notify[-MAX_NOTIFICATIONS_PER_RUN:]

        # Insurance: if no item moved the watermark (e.g. everything was already in
        # the db from another query), the next cycle would be a first run again and
        # re-fetch and re-flood all listings. Pin it explicitly.
        if data and db.get_last_timestamp(query_id) is None:
            db.update_last_timestamp(query_id, max(i.raw_timestamp for i in data))

        for item in to_notify:
            # The category is only on the item page, so it is read here, for announced items
            category = fetch_category(item)
            if category:
                db.set_item_category(item.id, category)
            # We create the message
            message_template = db.get_parameter("message_template")
            # Escaped because the message is sent with Telegram's HTML parse mode
            escaped_query_name = html.escape(query_name)
            content = message_template.format(
                title=item.title,
                price=str(item.price) + " " + item.currency,
                brand=item.brand_title,
                condition=html.escape(item.condition or ""),
                category=html.escape(category or ""),
                image=None if item.photo is None else item.photo,
                query=escaped_query_name,
            )
            # Templates saved before {query} existed would otherwise never show the query
            if "{query}" not in message_template:
                content = f"🔎 <b>{escaped_query_name}</b>\n{content}"
            # Same for {condition} and {category}: templates saved before them get them appended
            if "{condition}" not in message_template and item.condition:
                content += f"\n📦 Condition : {html.escape(item.condition)}"
            if "{category}" not in message_template and category:
                content += f"\n📂 Category : {html.escape(category)}"
            # add the item to the queue
            new_items_queue.put((content, item.url, "Open Vinted", None, None))
            # new_items_queue.put((content, item.url, "Open Vinted", item.buy_url, "Open buy page"))


def fetch_category(item):
    """
    Read the category of an item from its Vinted page.

    Any failure is logged and returns None, so the notification still goes out
    without a category.

    Args:
        item (Item): The item to look up

    Returns:
        str: The category title, or None if it could not be read
    """
    try:
        response = requester.get_page(item.url)
        response.raise_for_status()
        return category_from_page(response.text)
    except Exception as e:
        logger.warning(f"Could not read the category of item {item.id}: {str(e)}")
        return None


def contains_banwords(title, banwords_str):
    """
    Check if a title contains any banwords.

    Args:
        title (str): The title to check
        banwords_str (str): List of banwords separated by 3 pipe character
    Returns:
        bool: True if the title contains any banwords, False otherwise
    """

    # Split the banwords string into a list using pipe as delimiter
    banwords = [
        word.strip().lower() for word in banwords_str.split("|||") if word.strip()
    ]

    # If the list is empty, return False
    if not banwords:
        return False

    # Check if any banword is in the title (case-insensitive)
    title_lower = title.lower()
    for word in banwords:
        if word in title_lower:
            return True

    return False


def check_version():
    """
    Check if the application is up to date
    """
    try:
        # Get URL from the database
        github_url = db.get_parameter("github_url")
        # Get version from the database
        ver = db.get_parameter("version")
        # Get latest version from the repository
        url = f"{github_url}/releases/latest"
        response = requests.get(url)

        if response.status_code == 200:
            latest_version = response.url.split("/")[-1]
            is_up_to_date = ver == latest_version
            return is_up_to_date, ver, latest_version, github_url
        else:
            # If we can't check, assume it's up to date
            return True, ver, ver, github_url
    except Exception as e:
        logger.error(f"Error checking for new version: {str(e)}", exc_info=True)
        # If we can't check, assume it's up to date
        return True, ver, ver, github_url
