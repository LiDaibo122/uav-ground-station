"""Browser-facing configuration for the XD map. Only browser credentials belong here."""
import json
from urllib.parse import urlsplit


def valid_https(value):
    url = urlsplit(value)
    return (url.scheme == 'https' and url.hostname and not url.username
            and not url.password and not url.fragment)


def read_map_sources(path):
    config = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
    if not isinstance(config, dict):
        raise ValueError('底图配置必须是 JSON 对象')
    key = config.get('maptiler_api_key', '')
    if not isinstance(key, str) or len(key) > 512:
        raise ValueError('maptiler_api_key 必须是字符串')
    style = config.get('maplibre_style_url', '')
    if not isinstance(style, str) or (style.strip() and not valid_https(style.strip())):
        raise ValueError('maplibre_style_url 需要 HTTPS 样式地址或空字符串')
    custom = config.get('custom_xyz')
    if custom is not None:
        if not isinstance(custom, dict):
            raise ValueError('custom_xyz 必须是对象或 null')
        fields = ('name', 'url', 'attribution', 'attribution_url')
        if any(not isinstance(custom.get(field), str) or not custom[field].strip() for field in fields):
            raise ValueError('自定义瓦片需要 name、url、attribution 和 attribution_url')
        if (not all(valid_https(custom[k]) for k in ('url', 'attribution_url'))
                or not all(marker in custom['url'] for marker in ('{z}', '{x}', '{y}'))):
            raise ValueError('自定义瓦片需要 HTTPS 地址和 {z}/{x}/{y} 占位符')
        min_zoom, max_zoom = custom.get('min_zoom', 0), custom.get('max_zoom', 19)
        if type(min_zoom) is not int or type(max_zoom) is not int or not 0 <= min_zoom <= max_zoom <= 22:
            raise ValueError('自定义瓦片的缩放等级需要是 0 到 22 的整数')
        custom = {**{k: custom[k] for k in fields}, 'min_zoom': min_zoom, 'max_zoom': max_zoom}
    return {'maptiler_api_key': key.strip(), 'custom_xyz': custom,
            'maplibre_style_url': style.strip()}
