#!/usr/bin/env python3
"""
地点信息查询脚本（P0 改进版）
- API 降级策略：Overpass 失败 → 仅返回坐标和地址，跳过 POI
- Nominatim 速率控制：请求间 sleep 1 秒
- 数据质量标记：status 字段标记数据完整性
- 可达性信息：查询附近公共交通站点

用法:
    python3 location_query.py --location "上海武康路" [--radius 500]
    python3 location_query.py --location "鼓浪屿" --radius 1000

输出 JSON 格式的地点信息，包括坐标、周边设施、场地特征、可达性等。
"""

import argparse
import json
import sys
import time
import urllib.request
import urllib.parse
import urllib.error
from datetime import datetime


# Nominatim 请求时间戳，用于速率控制
_last_nominatim_time = 0


def nominatim_rate_limit():
    """Nominatim Usage Policy: 每秒不超过 1 次请求"""
    global _last_nominatim_time
    elapsed = time.time() - _last_nominatim_time
    if elapsed < 1.0:
        time.sleep(1.0 - elapsed)
    _last_nominatim_time = time.time()


def geocode_location(location):
    """使用 OpenStreetMap Nominatim 进行地理编码"""
    nominatim_rate_limit()
    query = urllib.parse.urlencode({"q": location, "format": "json", "limit": 1, "addressdetails": 1})
    url = f"https://nominatim.openstreetmap.org/search?{query}"
    req = urllib.request.Request(url, headers={
        "User-Agent": "PhotographyPlanSkill/1.0"
    })
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            if data and len(data) > 0:
                return {
                    "lat": float(data[0]["lat"]),
                    "lon": float(data[0]["lon"]),
                    "display_name": data[0].get("display_name", location),
                    "type": data[0].get("type", ""),
                    "class": data[0].get("class", ""),
                    "address": data[0].get("address", {}),
                    "boundingbox": data[0].get("boundingbox", []),
                }
    except Exception as e:
        print(f"地理编码失败: {e}", file=sys.stderr)
    return None


def query_nearby_pois(lat, lon, radius=500):
    """使用 Overpass API 查询周边 POI（兴趣点）"""
    overpass_query = f"""
    [out:json][timeout:15];
    (
        node["tourism"](around:{radius},{lat},{lon});
        node["leisure"](around:{radius},{lat},{lon});
        node["amenity"~"cafe|restaurant|bar|fast_food"](around:{radius},{lat},{lon});
        node["historic"](around:{radius},{lat},{lon});
        way["tourism"](around:{radius},{lat},{lon});
        way["leisure"](around:{radius},{lat},{lon});
        way["historic"](around:{radius},{lat},{lon});
    );
    out center 30;
    """

    url = "https://overpass-api.de/api/interpreter"
    data = urllib.parse.urlencode({"data": overpass_query}).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={
        "User-Agent": "PhotographyPlanSkill/1.0",
        "Content-Type": "application/x-www-form-urlencoded"
    })
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            elements = result.get("elements", [])

            pois = []
            for el in elements[:30]:
                tags = el.get("tags", {})
                name = tags.get("name", tags.get("name:zh", tags.get("name:en", "未命名")))
                poi_type = tags.get("tourism", tags.get("leisure", tags.get("amenity", tags.get("historic", "unknown"))))
                if name and name != "未命名":
                    pois.append({
                        "name": name,
                        "type": poi_type,
                        "subtype": tags.get("tourism", "") or tags.get("leisure", "") or tags.get("historic", ""),
                        "lat": el.get("lat", el.get("center", {}).get("lat")),
                        "lon": el.get("lon", el.get("center", {}).get("lon")),
                    })
            return pois
    except Exception as e:
        print(f"POI 查询失败: {e}", file=sys.stderr)
    return None  # 返回 None 而非空列表，以区分"失败"和"无结果"


def query_nearby_transport(lat, lon, radius=800):
    """查询附近的公共交通站点（地铁/公交）"""
    overpass_query = f"""
    [out:json][timeout:15];
    (
        node["railway"~"station|subway_entrance|tram_stop"](around:{radius},{lat},{lon});
        node["highway"="bus_stop"](around:{radius},{lat},{lon});
        node["public_transport"="platform"](around:{radius},{lat},{lon});
    );
    out 15;
    """

    url = "https://overpass-api.de/api/interpreter"
    data = urllib.parse.urlencode({"data": overpass_query}).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={
        "User-Agent": "PhotographyPlanSkill/1.0",
        "Content-Type": "application/x-www-form-urlencoded"
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            elements = result.get("elements", [])

            stops = []
            for el in elements[:15]:
                tags = el.get("tags", {})
                name = tags.get("name", tags.get("name:zh", "未命名站点"))
                stop_type = tags.get("railway", tags.get("highway", tags.get("public_transport", "unknown")))

                # 判断交通类型
                if stop_type in ["station", "subway_entrance"]:
                    transport_type = "地铁/火车站"
                elif stop_type == "tram_stop":
                    transport_type = "有轨电车站"
                elif stop_type == "bus_stop":
                    transport_type = "公交站"
                else:
                    transport_type = "公共交通站点"

                stops.append({
                    "name": name,
                    "type": transport_type,
                    "lat": el.get("lat"),
                    "lon": el.get("lon"),
                })
            return stops
    except Exception as e:
        print(f"交通站点查询失败: {e}", file=sys.stderr)
    return None


def get_location_summary(location_name, geo, pois, transport_stops):
    """生成地点摘要信息"""
    summary = {
        "query": location_name,
        "resolved_name": geo.get("display_name", location_name),
        "coordinates": {"lat": geo["lat"], "lon": geo["lon"]},
        "location_type": geo.get("type", ""),
        "address": geo.get("address", {}),
    }

    # 分类 POI
    if pois is not None:
        if len(pois) == 0:
            summary["nearby_points_of_interest"] = {}
            summary["poi_count"] = 0
        else:
            categories = {}
            for poi in pois:
                cat = poi.get("type", "other")
                if cat not in categories:
                    categories[cat] = []
                categories[cat].append(poi["name"])

            summary["nearby_points_of_interest"] = categories
            summary["poi_count"] = len(pois)

            # 推断场地特征
            all_types = [p.get("subtype", p.get("type", "")) for p in pois]
            features = []
            if any(t in ["park", "garden", "nature_reserve"] for t in all_types):
                features.append("有公园/绿地，适合自然光人像")
            if any(t in ["cafe", "restaurant", "bar"] for t in all_types):
                features.append("有餐饮场所，适合探店/生活方式拍摄")
            if any(t in ["attraction", "museum", "monument", "castle"] for t in all_types):
                features.append("有景点/历史建筑，适合人文/复古主题")
            if any(t in ["artwork", "gallery"] for t in all_types):
                features.append("有艺术空间，适合创意/文艺主题")

            if features:
                summary["site_features"] = features

    # 可达性信息
    if transport_stops is not None:
        if len(transport_stops) == 0:
            summary["accessibility"] = {"nearby_stops": [], "note": "周边 800m 内未找到公共交通站点"}
        else:
            # 按类型分组
            by_type = {}
            for stop in transport_stops:
                t = stop["type"]
                if t not in by_type:
                    by_type[t] = []
                by_type[t].append(stop["name"])
            summary["accessibility"] = {
                "nearby_stops": [{"type": t, "names": names} for t, names in by_type.items()],
                "stop_count": len(transport_stops),
            }

    return summary


def main():
    parser = argparse.ArgumentParser(description="查询拍摄地点的周边信息（P0 改进版）")
    parser.add_argument("--location", required=True, help="拍摄地点名称")
    parser.add_argument("--radius", type=int, default=500, help="搜索半径（米），默认 500")
    args = parser.parse_args()

    print(f"正在查询 {args.location} 的地点信息...", file=sys.stderr)

    # Step 1: 地理编码
    geo = geocode_location(args.location)
    if not geo:
        print(json.dumps({"error": f"无法找到地点: {args.location}", "status": "failed"}, ensure_ascii=False))
        sys.exit(1)

    print(f"地理位置: {geo['display_name']} ({geo['lat']}, {geo['lon']})", file=sys.stderr)

    # Step 2: 查询周边 POI（带降级）
    pois = query_nearby_pois(geo["lat"], geo["lon"], args.radius)
    poi_status = "ok" if pois is not None else "failed"
    if pois is None:
        print("Overpass POI 查询失败，降级为仅返回坐标和地址", file=sys.stderr)
        pois = []

    print(f"找到 {len(pois)} 个周边兴趣点", file=sys.stderr)

    # Step 3: 查询附近交通站点
    transport_stops = query_nearby_transport(geo["lat"], geo["lon"])
    transport_status = "ok" if transport_stops is not None else "failed"
    if transport_stops is None:
        print("交通站点查询失败，跳过可达性分析", file=sys.stderr)
        transport_stops = []

    # Step 4: 生成摘要
    summary = get_location_summary(args.location, geo, pois, transport_stops)

    # 综合数据质量状态
    if poi_status == "ok" and transport_status == "ok":
        overall_status = "ok"
    elif poi_status == "failed" and transport_status == "failed":
        overall_status = "degraded"
    else:
        overall_status = "degraded"

    summary["data_status"] = {
        "overall": overall_status,
        "geocode": "ok",
        "poi_query": poi_status,
        "transport_query": transport_status,
    }
    summary["query_time"] = datetime.now().isoformat()

    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
