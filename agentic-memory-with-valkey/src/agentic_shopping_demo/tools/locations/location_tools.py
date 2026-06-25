"""Location tools for ShopNow store lookup and geocoding."""

import json
import math
from pathlib import Path
from typing import Optional

import boto3
from botocore.exceptions import ClientError, NoCredentialsError
from strands import tool


@tool
def get_place_coordinates(search_string: str) -> str:
    """
    Get latitude and longitude coordinates for a place based on a search string.

    Use this to determine a customer's location when they provide an address,
    landmark, or place name, so nearby ShopNow stores can be found.

    Args:
        search_string: The place to search for (e.g., "Seattle, WA" or "1234 Main St, Portland")

    Returns:
        Formatted string with up to 3 matching locations including coordinates
    """
    try:
        session = boto3.Session(profile_name='default')
        location_client = session.client('location')

        response = location_client.search_place_index_for_text(
            IndexName='geocoding-index',
            Text=search_string,
            MaxResults=3
        )

        if not response.get('Results'):
            return f"No locations found for '{search_string}'"

        results = response['Results']
        output_lines = [f"Found {len(results)} result(s) for \"{search_string}\":\n"]

        for idx, result in enumerate(results, 1):
            place = result['Place']
            relevance = result.get('Relevance', 0) * 100
            coords = place['Geometry']['Point']
            longitude, latitude = coords[0], coords[1]
            label = place.get('Label', 'Unknown location')
            categories = place.get('Categories', [])
            category_str = ', '.join(categories) if categories else 'Unknown'

            output_lines.append(f"{idx}. {label}")
            output_lines.append(f"   Coordinates: Lat {latitude}, Long {longitude}")
            output_lines.append(f"   Type: {category_str}")
            output_lines.append(f"   Relevance: {relevance:.2f}%")
            if idx < len(results):
                output_lines.append("")

        return "\n".join(output_lines)

    except ClientError as e:
        return f"Location service error: {e.response['Error']['Code']} - {e.response['Error']['Message']}"
    except NoCredentialsError:
        return "Error: AWS credentials not configured."
    except Exception as e:
        return f"Unexpected error: {str(e)}"


_shopnow_locations_cache = None


def _load_shopnow_locations() -> list:
    """Load ShopNow store locations from JSON file (cached)."""
    global _shopnow_locations_cache
    if _shopnow_locations_cache is not None:
        return _shopnow_locations_cache

    current_dir = Path(__file__).parent
    locations_file = current_dir.parent.parent / 'assets' / 'locations.json'

    if not locations_file.exists():
        raise FileNotFoundError(f"Could not find locations.json at {locations_file}")

    with open(locations_file, 'r') as f:
        data = json.load(f)

    _shopnow_locations_cache = data.get('page', [])
    return _shopnow_locations_cache


def _haversine_distance(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Calculate great circle distance in miles between two coordinates."""
    lat1, lon1, lat2, lon2 = map(math.radians, [lat1, lon1, lat2, lon2])
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    a = math.sin(dlat/2)**2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon/2)**2
    return 3959.0 * 2 * math.asin(math.sqrt(a))


@tool
def find_nearby_shopnow_locations(
    latitude: float,
    longitude: float,
    max_results: int = 5,
    max_distance_miles: Optional[float] = None,
    has_curbside_pickup: Optional[bool] = None,
    in_store_pickup: Optional[bool] = None,
) -> str:
    """
    Find nearby ShopNow store locations sorted by distance from the customer.

    Use this when a customer asks about nearby stores, pickup options, or
    wants to know which ShopNow locations are closest to them.

    Args:
        latitude: Customer's latitude coordinate
        longitude: Customer's longitude coordinate
        max_results: Maximum number of stores to return (default: 5)
        max_distance_miles: Only show stores within this many miles
        has_curbside_pickup: Filter for stores with curbside pickup
        in_store_pickup: Filter for stores with in-store pickup

    Returns:
        Formatted list of nearby ShopNow stores with distance, address, and services
    """
    try:
        locations = _load_shopnow_locations()
        filtered = []

        for loc in locations:
            if has_curbside_pickup is not None and loc.get('hasCurbsidePickup', False) != has_curbside_pickup:
                continue
            if in_store_pickup is not None and loc.get('inStorePickup', False) != in_store_pickup:
                continue

            try:
                loc_lat = float(loc.get('latitude', 0))
                loc_lon = float(loc.get('longitude', 0))
                distance = _haversine_distance(latitude, longitude, loc_lat, loc_lon)
                if max_distance_miles is not None and distance > max_distance_miles:
                    continue
                loc_copy = loc.copy()
                loc_copy['calculated_distance'] = distance
                filtered.append(loc_copy)
            except (ValueError, TypeError):
                continue

        filtered.sort(key=lambda x: x['calculated_distance'])
        results = filtered[:max_results]

        if not results:
            return "No ShopNow stores found matching your criteria."

        distance_note = f" within {max_distance_miles} miles" if max_distance_miles else ""
        output_lines = [f"Found {len(results)} ShopNow store(s){distance_note}:\n"]

        for idx, loc in enumerate(results, 1):
            distance = loc['calculated_distance']
            site_name = loc.get('siteName', 'Unknown')
            site_code = loc.get('siteCode', 'N/A')
            address = loc.get('fullAddress', 'Address not available')

            output_lines.append(f"{idx}. {site_code} — {site_name} ({distance:.1f} miles)")
            output_lines.append(f"   Address: {address}")

            services = []
            if loc.get('hasCurbsidePickup', False):
                services.append("🚗 Curbside pickup")
            if loc.get('inStorePickup', False):
                services.append("🏪 In-store pickup")
            if services:
                output_lines.append(f"   Services: {', '.join(services)}")

            output_lines.append(f"   Coordinates: {loc.get('latitude', 'N/A')}, {loc.get('longitude', 'N/A')}")
            if idx < len(results):
                output_lines.append("")

        return "\n".join(output_lines)

    except FileNotFoundError as e:
        return f"Error: {str(e)}"
    except Exception as e:
        return f"Unexpected error: {str(e)}"


@tool
def lookup_shopnow_store(
    site_code: Optional[str] = None,
    store_name: Optional[str] = None,
) -> str:
    """
    Look up a specific ShopNow store by site code or store name.

    Use this when a customer mentions a specific store location by name or code.

    Args:
        site_code: Store site code (e.g., "SEA-001", "BEL-014"). Case-insensitive.
        store_name: Store name (e.g., "Seattle Downtown"). Supports partial matching.

    Returns:
        Store details including address, services, and pickup options
    """
    try:
        if site_code is None and store_name is None:
            return "Error: Please provide either a site_code or store_name."

        locations = _load_shopnow_locations()
        matches = []

        for loc in locations:
            if site_code is not None:
                if loc.get('siteCode', '').upper() != site_code.upper():
                    continue
            if store_name is not None:
                if store_name.lower() not in loc.get('siteName', '').lower():
                    continue
            matches.append(loc)

        if not matches:
            terms = []
            if site_code: terms.append(f'code="{site_code}"')
            if store_name: terms.append(f'name="{store_name}"')
            return f"No ShopNow stores found matching {' and '.join(terms)}"

        output_lines = [f"Found {len(matches)} ShopNow store(s):\n"]

        for idx, loc in enumerate(matches, 1):
            site_name = loc.get('siteName', 'Unknown')
            site_code_val = loc.get('siteCode', 'N/A')
            address = loc.get('fullAddress', 'Address not available')
            region = loc.get('subRegion', loc.get('region', 'Unknown'))

            output_lines.append(f"{site_code_val} — {site_name}")
            output_lines.append(f"   Address: {address}")
            output_lines.append(f"   Region: {region}")

            services = []
            if loc.get('hasCurbsidePickup', False):
                services.append("🚗 Curbside pickup")
            if loc.get('inStorePickup', False):
                services.append("🏪 In-store pickup")
            if loc.get('loanerAvailable', False):
                services.append("📦 Loaner items available")
            if services:
                output_lines.append(f"   Services: {', '.join(services)}")

            hours = loc.get('storeHours', {})
            if hours:
                output_lines.append(f"   Hours: {hours}")

            output_lines.append(f"   Coordinates: {loc.get('latitude', 'N/A')}, {loc.get('longitude', 'N/A')}")
            if idx < len(matches):
                output_lines.append("")

        return "\n".join(output_lines)

    except FileNotFoundError as e:
        return f"Error: {str(e)}"
    except Exception as e:
        return f"Unexpected error: {str(e)}"
