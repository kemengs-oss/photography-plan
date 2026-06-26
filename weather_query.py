#!/usr/bin/env python3
"""
天气与光线信息查询脚本（P0 改进版）
- 时段粒度天气：解析 wttr.in 3小时粒度预报
- API 降级策略：wttr.in 失败 → Open-Meteo；sunrise-sunset.org 失败 → 经验公式
- 时区自适应：根据经度自动估算时区
- 数据质量标记：status 字段标记数据完整性

用法:
    python3 weather_query.py --location "北京" [--date 2024-06-25]
    python3 weather_query.py --location "上海武康路" --date 2024-06-25

输出 JSON 格式的天气、温度、日照、黄金时刻、时段天气表等信息。
"""

import argparse
import json
import sys
import time
import math
import urllib.request
import urllib.parse
import urllib.error
from datetime import datetime, timedelta


def geocode_location(location):
    """使用 OpenStreetMap Nominatim 进行地理编码（免费、无需 API Key）"""
    query = urllib.parse.urlencode({"q": location, "format": "json", "limit": 1})
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
                    "display_name": data[0].get("display_name", location)
                }
    except Exception as e:
        print(f"地理编码失败: {e}", file=sys.stderr)
    return None


def estimate_timezone_offset(lon):
    """根据经度估算时区偏移（小时），每15度一个时区"""
    return round(lon / 15)


def get_weather_wttr(lat, lon):
    """使用 wttr.in 获取天气信息（主数据源）"""
    url = f"https://wttr.in/{lat},{lon}?format=j1"
    req = urllib.request.Request(url, headers={
        "User-Agent": "PhotographyPlanSkill/1.0"
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            current = data.get("current_condition", [{}])[0]
            weather_desc = current.get("weatherDesc", [{}])[0].get("value", "未知")

            today_weather = data.get("weather", [{}])
            today = today_weather[0] if today_weather else {}
            astronomy = today.get("astronomy", [{}])[0] if today.get("astronomy") else {}

            # 解析 3 小时粒度的时段预报
            hourly_forecast = []
            for day_idx, day_data in enumerate(today_weather[:2]):  # 今天 + 明天
                hourly = day_data.get("hourly", [])
                for h in hourly:
                    time_str = h.get("time", "").zfill(4)  # e.g. "900" -> "0900"
                    hour_val = int(time_str[:2]) if len(time_str) >= 2 else 0
                    date_str = day_data.get("date", "")
                    hourly_forecast.append({
                        "date": date_str,
                        "time": f"{hour_val:02d}:00",
                        "temp_c": h.get("tempC", "N/A"),
                        "weather_code": h.get("weatherCode", ""),
                        "weather_desc": h.get("weatherDesc", [{}])[0].get("value", "") if h.get("weatherDesc") else "",
                        "chance_of_rain": h.get("chanceofrain", "0"),
                        "cloud_cover": h.get("cloudcover", "N/A"),
                        "uv_index": h.get("uvIndex", "N/A"),
                        "visibility_km": h.get("visibility", "N/A"),
                        "wind_kmph": h.get("windspeedKmph", "N/A"),
                    })

            return {
                "source": "wttr.in",
                "current": {
                    "temp_c": current.get("temp_C", "N/A"),
                    "feels_like_c": current.get("FeelsLikeC", "N/A"),
                    "humidity": current.get("humidity", "N/A"),
                    "weather_desc": weather_desc,
                    "wind_kmph": current.get("windspeedKmph", "N/A"),
                    "visibility_km": current.get("visibility", "N/A"),
                    "cloud_cover": current.get("cloudcover", "N/A"),
                    "uv_index": current.get("uvIndex", "N/A"),
                },
                "today": {
                    "max_temp_c": today.get("maxtempC", "N/A"),
                    "min_temp_c": today.get("mintempC", "N/A"),
                    "avg_temp_c": today.get("avgtempC", "N/A"),
                    "sunrise": astronomy.get("sunrise", "N/A"),
                    "sunset": astronomy.get("sunset", "N/A"),
                    "moon_phase": astronomy.get("moon_phase", "N/A"),
                },
                "hourly_forecast": hourly_forecast,
            }
    except Exception as e:
        print(f"wttr.in 天气查询失败: {e}", file=sys.stderr)
    return None


def get_weather_openmeteo(lat, lon):
    """使用 Open-Meteo API 获取天气信息（降级数据源）"""
    params = urllib.parse.urlencode({
        "latitude": lat,
        "longitude": lon,
        "current": "temperature_2m,relative_humidity_2m,weather_code,wind_speed_10m,cloud_cover,uv_index",
        "daily": "temperature_2m_max,temperature_2m_min,sunrise,sunset",
        "hourly": "temperature_2m,weather_code,cloud_cover,uv_index,visibility,wind_speed_10m,precipitation_probability",
        "timezone": "auto",
        "forecast_days": 2,
    })
    url = f"https://api.open-meteo.com/v1/forecast?{params}"
    req = urllib.request.Request(url, headers={
        "User-Agent": "PhotographyPlanSkill/1.0"
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))

            current = data.get("current", {})
            daily = data.get("daily", {})
            hourly = data.get("hourly", {})

            # WMO weather code 转描述
            wmo_codes = {
                0: "晴", 1: "大部晴", 2: "多云", 3: "阴",
                45: "雾", 48: "冻雾",
                51: "小雨", 53: "中雨", 55: "大雨",
                61: "小雨", 63: "中雨", 65: "大雨",
                71: "小雪", 73: "中雪", 75: "大雪",
                80: "阵雨", 81: "中阵雨", 82: "大阵雨",
                95: "雷暴", 96: "雷暴冰雹", 99: "强雷暴冰雹",
            }

            # 构建时段预报
            hourly_forecast = []
            hourly_times = hourly.get("time", [])
            for i, t in enumerate(hourly_times):
                wmo = hourly.get("weather_code", [])[i] if i < len(hourly.get("weather_code", [])) else 0
                hourly_forecast.append({
                    "date": t[:10],
                    "time": t[11:16] if len(t) > 11 else "00:00",
                    "temp_c": str(hourly.get("temperature_2m", [])[i]) if i < len(hourly.get("temperature_2m", [])) else "N/A",
                    "weather_code": str(wmo),
                    "weather_desc": wmo_codes.get(wmo, "未知"),
                    "chance_of_rain": str(hourly.get("precipitation_probability", [])[i]) if i < len(hourly.get("precipitation_probability", [])) else "0",
                    "cloud_cover": str(hourly.get("cloud_cover", [])[i]) if i < len(hourly.get("cloud_cover", [])) else "N/A",
                    "uv_index": str(hourly.get("uv_index", [])[i]) if i < len(hourly.get("uv_index", [])) else "N/A",
                    "visibility_km": str(hourly.get("visibility", [])[i]) if i < len(hourly.get("visibility", [])) else "N/A",
                    "wind_kmph": str(hourly.get("wind_speed_10m", [])[i]) if i < len(hourly.get("wind_speed_10m", [])) else "N/A",
                })

            cur_wmo = current.get("weather_code", 0)
            return {
                "source": "open-meteo",
                "current": {
                    "temp_c": str(current.get("temperature_2m", "N/A")),
                    "feels_like_c": "N/A",
                    "humidity": str(current.get("relative_humidity_2m", "N/A")),
                    "weather_desc": wmo_codes.get(cur_wmo, "未知"),
                    "wind_kmph": str(current.get("wind_speed_10m", "N/A")),
                    "visibility_km": "N/A",
                    "cloud_cover": str(current.get("cloud_cover", "N/A")),
                    "uv_index": str(current.get("uv_index", "N/A")),
                },
                "today": {
                    "max_temp_c": str(daily.get("temperature_2m_max", ["N/A"])[0]) if daily.get("temperature_2m_max") else "N/A",
                    "min_temp_c": str(daily.get("temperature_2m_min", ["N/A"])[0]) if daily.get("temperature_2m_min") else "N/A",
                    "avg_temp_c": "N/A",
                    "sunrise": daily.get("sunrise", ["N/A"])[0] if daily.get("sunrise") else "N/A",
                    "sunset": daily.get("sunset", ["N/A"])[0] if daily.get("sunset") else "N/A",
                    "moon_phase": "N/A",
                },
                "hourly_forecast": hourly_forecast,
            }
    except Exception as e:
        print(f"Open-Meteo 天气查询失败: {e}", file=sys.stderr)
    return None


def get_weather_with_fallback(lat, lon):
    """天气查询：wttr.in → Open-Meteo 降级"""
    weather = get_weather_wttr(lat, lon)
    if weather:
        return weather, "ok"

    print("wttr.in 不可用，降级到 Open-Meteo...", file=sys.stderr)
    weather = get_weather_openmeteo(lat, lon)
    if weather:
        return weather, "degraded"

    return None, "failed"


def get_sunrise_sunset_api(lat, lon, date_str=None):
    """使用 sunrise-sunset.org 获取精确的日出日落（主数据源）"""
    if date_str:
        try:
            date_obj = datetime.strptime(date_str, "%Y-%m-%d")
        except ValueError:
            date_obj = datetime.now()
    else:
        date_obj = datetime.now()

    formatted_date = date_obj.strftime("%Y-%m-%d")
    query = urllib.parse.urlencode({
        "lat": lat,
        "lng": lon,
        "date": formatted_date,
        "formatted": "0"
    })
    url = f"https://api.sunrise-sunset.org/json?{query}"
    req = urllib.request.Request(url, headers={
        "User-Agent": "PhotographyPlanSkill/1.0"
    })
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            if data.get("status") == "OK":
                results = data["results"]
                return {
                    "sunrise": results.get("sunrise", "N/A"),
                    "sunset": results.get("sunset", "N/A"),
                    "solar_noon": results.get("solar_noon", "N/A"),
                    "civil_twilight_begin": results.get("civil_twilight_begin", "N/A"),
                    "civil_twilight_end": results.get("civil_twilight_end", "N/A"),
                    "nautical_twilight_begin": results.get("nautical_twilight_begin", "N/A"),
                    "nautical_twilight_end": results.get("nautical_twilight_end", "N/A"),
                    "day_length": results.get("day_length", "N/A"),
                }
    except Exception as e:
        print(f"sunrise-sunset.org 查询失败: {e}", file=sys.stderr)
    return None


def calculate_sunrise_sunset_empirical(lat, lon, date_str=None):
    """
    经验公式计算日出日落（降级方案）
    基于太阳赤纬和时角的计算，精度约 ±15 分钟
    """
    if date_str:
        try:
            date_obj = datetime.strptime(date_str, "%Y-%m-%d")
        except ValueError:
            date_obj = datetime.now()
    else:
        date_obj = datetime.now()

    # 计算一年中的第几天
    day_of_year = date_obj.timetuple().tm_yday

    # 太阳赤纬（declination），Spencer 公式简化版
    gamma = 2 * math.pi * (day_of_year - 1) / 365
    declination = 0.006918 - 0.399912 * math.cos(gamma) + 0.070257 * math.sin(gamma) \
                  - 0.006758 * math.cos(2 * gamma) + 0.000907 * math.sin(2 * gamma) \
                  - 0.002697 * math.cos(3 * gamma) + 0.001480 * math.sin(3 * gamma)

    lat_rad = math.radians(lat)

    # 时角（sunrise hour angle）
    try:
        cos_omega = -math.tan(lat_rad) * math.tan(declination)
        cos_omega = max(-1, min(1, cos_omega))  # clamp
        omega = math.acos(cos_omega)
    except (ValueError, ZeroDivisionError):
        return None

    # 日出日落时间（UTC）
    sunrise_utc_minutes = 720 - 4 * math.degrees(omega) - 4 * lon  # 分钟
    sunset_utc_minutes = 720 + 4 * math.degrees(omega) - 4 * lon

    # 转为本地时区
    tz_offset = estimate_timezone_offset(lon)
    sunrise_local_minutes = sunrise_utc_minutes + tz_offset * 60
    sunset_local_minutes = sunset_utc_minutes + tz_offset * 60

    # 格式化
    def minutes_to_iso(minutes):
        total = int(minutes) % (24 * 60)
        h = total // 60
        m = total % 60
        date_str_fmt = date_obj.strftime("%Y-%m-%d")
        return f"{date_str_fmt}T{h:02d}:{m:02d}:00+{tz_offset:02d}:00"

    sunrise_iso = minutes_to_iso(sunrise_local_minutes)
    sunset_iso = minutes_to_iso(sunset_local_minutes)

    return {
        "sunrise": sunrise_iso,
        "sunset": sunset_iso,
        "solar_noon": minutes_to_iso((sunrise_local_minutes + sunset_local_minutes) / 2),
        "civil_twilight_begin": "N/A",
        "civil_twilight_end": "N/A",
        "nautical_twilight_begin": "N/A",
        "nautical_twilight_end": "N/A",
        "day_length": f"{int((sunset_local_minutes - sunrise_local_minutes) / 60)}:{int((sunset_local_minutes - sunrise_local_minutes) % 60):02d}",
    }


def get_sunrise_sunset_with_fallback(lat, lon, date_str=None):
    """日出日落查询：sunrise-sunset.org → 经验公式降级"""
    sun = get_sunrise_sunset_api(lat, lon, date_str)
    if sun:
        return sun, "ok"

    print("sunrise-sunset.org 不可用，使用经验公式计算...", file=sys.stderr)
    sun = calculate_sunrise_sunset_empirical(lat, lon, date_str)
    if sun:
        return sun, "degraded"

    return None, "failed"


def calculate_golden_blue_hours(sunrise_iso, sunset_iso, tz_offset=8):
    """根据日出日落时间计算黄金时刻和蓝调时刻（自动时区）"""
    result = {}
    try:
        from datetime import timezone, timedelta as tz_timedelta
        local_tz = timezone(tz_timedelta(hours=tz_offset))

        if sunrise_iso and sunrise_iso != "N/A":
            sr = datetime.fromisoformat(sunrise_iso.replace("Z", "+00:00")).astimezone(local_tz)
            golden_hour_morning_start = sr - timedelta(minutes=10)
            golden_hour_morning_end = sr + timedelta(minutes=50)
            blue_hour_morning_start = sr - timedelta(minutes=35)
            blue_hour_morning_end = sr - timedelta(minutes=5)
            result["golden_hour_morning"] = f"{golden_hour_morning_start.strftime('%H:%M')} - {golden_hour_morning_end.strftime('%H:%M')}"
            result["blue_hour_morning"] = f"{blue_hour_morning_start.strftime('%H:%M')} - {blue_hour_morning_end.strftime('%H:%M')}"

        if sunset_iso and sunset_iso != "N/A":
            ss = datetime.fromisoformat(sunset_iso.replace("Z", "+00:00")).astimezone(local_tz)
            golden_hour_evening_start = ss - timedelta(minutes=50)
            golden_hour_evening_end = ss + timedelta(minutes=10)
            blue_hour_evening_start = ss + timedelta(minutes=5)
            blue_hour_evening_end = ss + timedelta(minutes=35)
            result["golden_hour_evening"] = f"{golden_hour_evening_start.strftime('%H:%M')} - {golden_hour_evening_end.strftime('%H:%M')}"
            result["blue_hour_evening"] = f"{blue_hour_evening_start.strftime('%H:%M')} - {blue_hour_evening_end.strftime('%H:%M')}"
    except Exception as e:
        print(f"黄金时刻计算失败: {e}", file=sys.stderr)
    return result


def extract_shooting_window_weather(hourly_forecast, golden_blue):
    """
    从时段预报中提取拍摄关键时段的天气信息
    返回拍摄窗口（黄金时刻/蓝调时刻）的详细天气
    """
    if not hourly_forecast or not golden_blue:
        return []

    windows = []

    # 提取各时段
    for window_name, time_range in golden_blue.items():
        if not time_range or "N/A" in time_range:
            continue
        try:
            parts = time_range.split(" - ")
            start_hour = int(parts[0].split(":")[0])
            end_hour = int(parts[1].split(":")[0])

            # 找到覆盖该时段的 hourly 条目
            matching = []
            for h in hourly_forecast:
                try:
                    h_hour = int(h.get("time", "00:00").split(":")[0])
                    if start_hour <= h_hour <= end_hour or (start_hour > end_hour and (h_hour >= start_hour or h_hour <= end_hour)):
                        matching.append(h)
                except (ValueError, IndexError):
                    continue

            if matching:
                windows.append({
                    "window": window_name,
                    "time_range": time_range,
                    "weather_samples": matching,
                    "summary": {
                        "temp_range": f"{min(float(h.get('temp_c', 0)) for h in matching if h.get('temp_c', 'N/A') != 'N/A'):.0f} - {max(float(h.get('temp_c', 0)) for h in matching if h.get('temp_c', 'N/A') != 'N/A'):.0f}°C" if matching else "N/A",
                        "cloud_cover_avg": f"{sum(float(h.get('cloud_cover', 0)) for h in matching if h.get('cloud_cover', 'N/A') != 'N/A') / len(matching):.0f}%" if matching else "N/A",
                        "rain_chance_max": f"{max(int(h.get('chance_of_rain', 0)) for h in matching if h.get('chance_of_rain', '0').isdigit())}%" if matching else "N/A",
                    }
                })
        except (ValueError, IndexError):
            continue

    return windows


def main():
    parser = argparse.ArgumentParser(description="查询拍摄地点的天气与光线信息（P0 改进版）")
    parser.add_argument("--location", required=True, help="拍摄地点（中文或英文均可）")
    parser.add_argument("--date", default=None, help="日期 (YYYY-MM-DD)，默认今天")
    args = parser.parse_args()

    print(f"正在查询 {args.location} 的天气与光线信息...", file=sys.stderr)

    # Step 1: 地理编码
    geo = geocode_location(args.location)
    if not geo:
        print(json.dumps({"error": f"无法找到地点: {args.location}", "status": "failed"}, ensure_ascii=False))
        sys.exit(1)

    print(f"地理位置: {geo['display_name']} ({geo['lat']}, {geo['lon']})", file=sys.stderr)

    # 时区自适应
    tz_offset = estimate_timezone_offset(geo["lon"])
    print(f"估算时区: UTC{'+' if tz_offset >= 0 else ''}{tz_offset}", file=sys.stderr)

    # Step 2: 查询天气（带降级）
    weather, weather_status = get_weather_with_fallback(geo["lat"], geo["lon"])

    # Step 3: 查询日出日落（带降级）
    sun, sun_status = get_sunrise_sunset_with_fallback(geo["lat"], geo["lon"], args.date)

    # Step 4: 计算黄金时刻和蓝调时刻（自动时区）
    golden_blue = {}
    if sun:
        golden_blue = calculate_golden_blue_hours(
            sun.get("sunrise"),
            sun.get("sunset"),
            tz_offset
        )

    # Step 5: 提取拍摄窗口天气
    shooting_windows = []
    if weather and weather.get("hourly_forecast") and golden_blue:
        shooting_windows = extract_shooting_window_weather(
            weather["hourly_forecast"],
            golden_blue
        )

    # 综合数据质量状态
    if weather_status == "ok" and sun_status == "ok":
        overall_status = "ok"
    elif weather_status == "failed" and sun_status == "failed":
        overall_status = "failed"
    else:
        overall_status = "degraded"

    # 汇总输出
    result = {
        "location": {
            "query": args.location,
            "resolved_name": geo["display_name"],
            "lat": geo["lat"],
            "lon": geo["lon"],
            "estimated_timezone": f"UTC{'+' if tz_offset >= 0 else ''}{tz_offset}",
        },
        "weather": weather,
        "sun_info": sun,
        "golden_blue_hours": golden_blue,
        "shooting_window_weather": shooting_windows,
        "data_status": {
            "overall": overall_status,
            "weather_source": weather.get("source", "N/A") if weather else "failed",
            "sun_source": "sunrise-sunset.org" if sun_status == "ok" else ("empirical" if sun_status == "degraded" else "failed"),
            "hourly_forecast_available": bool(weather and weather.get("hourly_forecast")),
        },
        "query_time": datetime.now().isoformat(),
    }

    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
