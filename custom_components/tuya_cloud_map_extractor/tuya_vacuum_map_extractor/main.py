"""Downloads and renders vacuum map from Tuya servers."""
import base64
import requests
import math
import json
import logging
from requests.exceptions import JSONDecodeError, RequestException
from datetime import datetime
from PIL import Image, ImageDraw
from .v0 import decode_v0, to_array_v0
from .v1 import decode_v1, to_array_v1, decode_path_v1, _format_path_point
from .custom0 import decode_custom0, to_array_custom0, decode_path_custom0, map_to_image
from .tuya import get_download_link
from .const import NotSupportedError
from .common import decode_header

_LOGGER = logging.getLogger(__name__)

def download(url: str, timeout: float = 10.0) -> requests.models.Response:
    """Downloads map with increased timeout and better error handling."""
    try:
        response = requests.get(url=url, timeout=timeout)
        response.raise_for_status()
        return response
    except RequestException as e:
        _LOGGER.error(f"Download failed for URL {url}: {str(e)}")
        raise

def parse_map(response: requests.models.Response):
    """Parse map data with improved error handling and logging."""
    try:
        data = response.json()
        _LOGGER.debug(f"Raw JSON response: {json.dumps(data, indent=2)}")
        
        # Try different possible response formats
        if 'result' in data and isinstance(data['result'], list):
            # Standard Tuya response format
            header, mapDataArr = decode_custom0(data)
        elif 'map_data' in data:
            # Alternative format some devices use
            header, mapDataArr = decode_custom0(data['map_data'])
        else:
            # Fallback to raw binary parsing
            raise JSONDecodeError("No recognized JSON format", "", 0)

    except JSONDecodeError:
        _LOGGER.debug("Falling back to binary parsing")
        try:
            data = response.content.hex()
            header = decode_header(data[0:48])
            if header["version"] == [0]:
                mapDataArr = decode_v0(data, header)
            elif header["version"] == [1]:
                mapDataArr = decode_v1(data, header)
            else:
                raise NotSupportedError(f"Map version {header['version']} is not supported.")
        except Exception as e:
            _LOGGER.error(f"Binary parsing failed: {str(e)}")
            raise ValueError("Failed to parse map data in any supported format") from e
        
    return header, mapDataArr

def parse_path(response: requests.models.Response, scale=2.0, header={}):
    """Parse path data with improved error handling."""
    try:
        data = response.json()
        path_data = decode_path_custom0(data, header)
    except JSONDecodeError:
        try:
            data = response.content.hex()
            path_data = decode_path_v1(data)
        except Exception as e:
            _LOGGER.error(f"Path parsing failed: {str(e)}")
            return []

    coords = []
    for coord in path_data:
        for i in coord:
            coords.append(i*scale)

    return coords

def flip(headers: dict, image: Image.Image, settings: dict):
    """Apply image transformations with bounds checking."""
    try:
        rotate = settings.get("rotate", 0)
        flip_vertical = settings.get("flip_vertical", False)
        flip_horizontal = settings.get("flip_horizontal", False)
        
        if rotate in [90, 180, -90, 270]:
            image = image.rotate(rotate)
        if flip_vertical:
            image = image.transpose(Image.FLIP_LEFT_RIGHT)
        if flip_horizontal:
            image = image.transpose(Image.FLIP_TOP_BOTTOM)
            
    except Exception as e:
        _LOGGER.error(f"Image transformation failed: {str(e)}")
        
    return headers, image

def render_layout(raw_map: bytes, header: dict, colors: dict) -> Image.Image:
    """Render layout with version-specific handling."""
    try:
        width = header["width"]
        height = header["height"]
        
        if isinstance(header["version"], list):
            protoVer = str(header["version"][0])
        else:
            protoVer = header["version"]

        pixellist = list(raw_map)

        if protoVer == "custom0":
            array = to_array_custom0(pixellist, width, height, colors)
        elif protoVer == "0":
            array = to_array_v0(pixellist, width, height, colors)
        elif protoVer == "1":
            rooms = header.get("roominfo", [])
            array = to_array_v1(pixellist, width, height, rooms, colors)
        else:
            raise NotSupportedError(f"Protocol version {protoVer} not supported")

        return Image.fromarray(array)
        
    except Exception as e:
        _LOGGER.error(f"Rendering failed: {str(e)}")
        raise

def get_map(
    server: str,
    client_id: str,
    secret_key: str,
    device_id: str,
    colors=None,
    settings=None,
    urls=None
) -> tuple:
    """Main function with comprehensive error handling."""
    if colors is None:
        colors = {}
    if settings is None:
        settings = {}
    if urls is None:
        urls = {}

    try:
        # Get download links
        if not urls:
            link = get_download_link(server, client_id, secret_key, device_id)
            _LOGGER.debug(f"Download link response: {link}")
            
            if not link.get("result") or not isinstance(link["result"], list):
                _LOGGER.error("Invalid link response structure")
                raise ValueError("Invalid API response format")
                
            urls = {
                "links": link["result"],
                "time": datetime.now().strftime("%H:%M:%S"),
            }
        
        # Download map data
        map_link = next(
            (item["map_url"] for item in urls["links"] if "map_url" in item),
            None
        )
        
        if not map_link:
            _LOGGER.error("No map URL found in response")
            raise ValueError("No map URL available")

        response = download(map_link)
        _LOGGER.debug(f"Map response status: {response.status_code}")

        # Parse and render map
        header, mapDataArr = parse_map(response)
        image = render_layout(raw_map=mapDataArr, header=header, colors=colors)
        
        # Handle path rendering if enabled
        if settings.get("path_enabled", False):
            path_link = next(
                (item["map_url"] for item in urls["links"][1:] if "map_url" in item),
                None
            )
            
            if path_link:
                try:
                    path_response = download(path_link)
                    scale = int(1080/image.size[0])
                    image = image.resize(
                        (image.size[0]*scale, image.size[1]*scale),
                        resample=Image.BOX
                    )
                    
                    path = parse_path(path_response, scale=scale, header=header)
                    if path:
                        draw = ImageDraw.Draw(image, 'RGBA')
                        draw.line(
                            path,
                            fill=tuple(colors.get("path_color", [0, 255, 0])),
                            width=2
                        )
                        
                        # Draw charging station
                        if "pileX" in header and "pileY" in header:
                            x, y = header["pileX"], header["pileY"]
                            if header["version"] in [[0], [1]]:
                                point = _format_path_point({'x': x, 'y': y}, False)
                            else:
                                point = map_to_image(
                                    [x, y],
                                    header["mapResolution"],
                                    header["x_min"],
                                    header["y_min"]
                                )
                            x, y = point[0]*scale, point[1]*scale
                            draw.ellipse(
                                [(x-10, y-10), (x+10, y+10)],
                                outline=(255, 255, 255),
                                fill=(0, 255, 0),
                                width=2
                            )
                except Exception as e:
                    _LOGGER.error(f"Path rendering failed: {str(e)}")

        return flip(header, image, settings)
        
    except Exception as e:
        _LOGGER.error(f"Map processing failed: {str(e)}", exc_info=True)
        raise ValueError("Failed to process map data") from e
