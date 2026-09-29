"""5sim.net 接码平台 API 封装。

文档：https://5sim.net/zh/payment  →  https://5sim.net/en/docs
价格接口无需鉴权，购买/检查/取消/复用需要 API key（Bearer token）。

复用池持久化到 output/num5sim_pool.json，只保存号码供给与订单元数据。
Codex 每号最多绑定三个不同账号的额度由 MySQL AccountStore 决定。
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.http_utils import open_url as _open_url

BASE_URL = "https://5sim.net"
DEFAULT_MAX_USES = 3
DEFAULT_PRODUCT = "openai"
DEFAULT_OPERATOR = "any"

# 复用池文件路径（相对于项目根目录）
POOL_FILE_NAME = "num5sim_pool.json"

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


class FiveSimError(RuntimeError):
    """Base 5sim error."""


class FiveSimNoFreePhonesError(FiveSimError):
    """Raised when 5sim has no free phones for the requested filter."""


class FiveSimBalanceError(FiveSimError):
    """Raised when the 5sim account balance is insufficient."""


class FiveSimGatewayError(FiveSimError):
    """A gateway failed after a request may already have reached the provider."""


def _raise_text_error(path: str, text: str) -> None:
    stripped = text.strip()
    lowered = stripped.lower()
    if "502 bad gateway" in lowered or "504 gateway timeout" in lowered:
        raise FiveSimGatewayError("5sim 网关返回 502/504，请求结果尚未确认")
    if "no free phones" in lowered:
        raise FiveSimNoFreePhonesError(
            f"5sim 暂无可用号码 path={path}: {stripped}"
        )
    if "not enough user balance" in lowered or "not enough balance" in lowered:
        raise FiveSimBalanceError(
            f"5sim 余额不足 path={path}: {stripped}"
        )
    snippet = stripped[:300]
    raise FiveSimError(f"5sim API 返回非 JSON 响应 path={path}: {snippet}")


def _get(
    path: str,
    *,
    api_key: str = "",
    proxy: Optional[str] = None,
    proxy_insecure: bool = False,
    timeout: float = 30.0,
) -> Dict[str, Any]:
    headers = {
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    }
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    req = urllib.request.Request(BASE_URL + path, headers=headers)
    try:
        with _open_url(req, proxy=proxy, insecure=proxy_insecure, timeout=timeout) as resp:
            raw = resp.read()
            if not raw:
                raise FiveSimError(f"5sim API 返回空白响应 path={path}")
            decoded = raw.decode("utf-8", errors="replace")
            if not decoded.strip():
                raise FiveSimError(f"5sim API 返回空白响应 path={path}")
            return json.loads(decoded)
    except urllib.error.HTTPError as e:
        if e.code in {502, 504}:
            raise FiveSimGatewayError(f"5sim 网关返回 HTTP {e.code}，请求结果尚未确认") from e
        body = e.read().decode("utf-8", errors="replace")[:500]
        try:
            _raise_text_error(path, body)
        except FiveSimError as semantic:
            raise semantic from e
        raise FiveSimError(f"5sim API HTTP {e.code} {e.reason}: {body}") from e
    except urllib.error.URLError as e:
        raise FiveSimError(f"5sim API 网络错误: {e.reason}") from e
    except json.JSONDecodeError as e:
        try:
            _raise_text_error(path, decoded)
        except FiveSimError as semantic:
            raise semantic from e
        raise FiveSimError(f"5sim API 返回非 JSON 响应 path={path}: {e}\n原始响应: {raw[:300]!r}") from e


def _project_output_dir() -> Path:
    from pathlib import Path as _Path
    return (_Path(__file__).resolve().parent.parent / "output")


# ---------------------------------------------------------------------------
# data types
# ---------------------------------------------------------------------------


@dataclass
class PriceEntry:
    country: str
    operator: str
    product: str
    cost: float          # price in account currency
    count: int           # available numbers
    rate: float          # delivery rate (0.0–100.0)

    @property
    def rate_str(self) -> str:
        return f"{self.rate}%" if self.rate > 0 else "?"

    @property
    def sort_key(self) -> tuple:
        """rate 高 → cost 低 → count 多"""
        return (-self.rate, self.cost, -self.count)


@dataclass
class ActivationOrder:
    id: int
    phone: str
    operator: str
    product: str
    price: float
    status: str
    expires: str
    sms: Optional[List[Dict[str, Any]]] = None
    country: str = ""
    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def code(self) -> Optional[str]:
        if not self.sms:
            return None
        for msg in self.sms:
            c = msg.get("code")
            if c:
                return str(c)
        return None


@dataclass
class PoolEntry:
    """复用池中的一条记录。"""
    phone: str
    country: str
    operator: str
    product: str
    successful_uses: int = 0
    max_uses: int = DEFAULT_MAX_USES
    last_used_at: float = 0.0
    last_order_id: int = 0

    @property
    def usable(self) -> bool:
        return bool(self.phone)

    @property
    def remaining(self) -> int:
        return max(0, self.max_uses - self.successful_uses)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "phone": self.phone,
            "country": self.country,
            "operator": self.operator,
            "product": self.product,
            "successful_uses": self.successful_uses,
            "max_uses": self.max_uses,
            "last_used_at": self.last_used_at,
            "last_order_id": self.last_order_id,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "PoolEntry":
        return cls(
            phone=str(d.get("phone", "")),
            country=str(d.get("country", "")),
            operator=str(d.get("operator", "")),
            product=str(d.get("product", DEFAULT_PRODUCT)),
            successful_uses=int(d.get("successful_uses", 0)),
            max_uses=int(d.get("max_uses", DEFAULT_MAX_USES)),
            last_used_at=float(d.get("last_used_at", 0)),
            last_order_id=int(d.get("last_order_id", 0)),
        )


# ---------------------------------------------------------------------------
# 复用池持久化
# ---------------------------------------------------------------------------


class ActivationPool:
    """管理可复用号码池，持久化到 output/num5sim_pool.json。"""

    def __init__(self, entries: Optional[List[PoolEntry]] = None) -> None:
        self.entries: List[PoolEntry] = entries or []

    @property
    def pool_path(self) -> Path:
        return _project_output_dir() / POOL_FILE_NAME

    def save(self) -> None:
        self.pool_path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "updated_at": time.time(),
            "entries": [e.to_dict() for e in self.entries],
        }
        self.pool_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    @classmethod
    def load(cls) -> "ActivationPool":
        path = _project_output_dir() / POOL_FILE_NAME
        if not path.is_file():
            return cls()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return cls()
        entries = [PoolEntry.from_dict(d) for d in data.get("entries", [])]
        return cls(entries)

    def find_usable(
        self, country: str = "", operator: str = "", product: str = DEFAULT_PRODUCT,
        *, exclude_phones: Optional[set[str]] = None,
    ) -> Optional[PoolEntry]:
        """查找可复用号码；排除集合由账号绑定的权威 store 提供。"""
        excluded = {str(phone).strip() for phone in (exclude_phones or set())}
        candidates = [
            e for e in self.entries
            if e.phone not in excluded and e.product == product
            and (not country or e.country == country)
            and (not operator or e.operator == operator)
        ]
        if not candidates and not country and not operator:
            candidates = [e for e in self.entries if e.phone not in excluded and e.product == product]
        candidates.sort(key=lambda e: -e.last_used_at)
        return candidates[0] if candidates else None

    def add_or_update(self, phone: str, country: str, operator: str, product: str, order_id: int = 0) -> PoolEntry:
        """记录复用订单元数据；successful_uses 仅为兼容显示，不作 Codex 额度权威。"""
        for e in self.entries:
            if e.phone == phone and e.product == product:
                e.successful_uses += 1
                e.last_used_at = time.time()
                e.last_order_id = order_id
                return e
        entry = PoolEntry(
            phone=phone, country=country, operator=operator, product=product,
            successful_uses=1, last_used_at=time.time(), last_order_id=order_id,
        )
        self.entries.append(entry)
        return entry

    def remove(self, phone: str, product: str = DEFAULT_PRODUCT) -> None:
        self.entries = [e for e in self.entries if not (e.phone == phone and e.product == product)]

    def prune_expired(self) -> int:
        """Drop invalid inventory entries; historical use counts never expire a phone."""
        before = len(self.entries)
        self.entries = [e for e in self.entries if e.phone]
        return before - len(self.entries)

    def to_list(self) -> List[Dict[str, Any]]:
        return [e.to_dict() for e in self.entries]


# ---------------------------------------------------------------------------
# 价格 / 查询（无需鉴权）
# ---------------------------------------------------------------------------


def _parse_price_entries(
    data: Dict[str, Any],
    *,
    product_filter: str = "",
    country_filter: str = "",
) -> List[PriceEntry]:
    """解析 5sim /v1/guest/prices 响应，适配不同的查询参数组合。

    - 只按 product 过滤时，响应结构为 {product: {country: {operator: {cost,count,rate}}}}
    - 只按 country 过滤时，结构为 {country: {product: {operator: {cost,count,rate}}}}
    - 两者都过滤或无过滤时，默认为 {country: {product: {operator: {cost,count,rate}}}}
    """
    entries: List[PriceEntry] = []
    has_product = bool(product_filter and product_filter != "other")
    has_country = bool(country_filter)
    product_outer = has_product and not has_country

    for outer_key, middle_dict in data.items():
        if not isinstance(middle_dict, dict):
            continue
        for middle_key, operators in middle_dict.items():
            if not isinstance(operators, dict):
                continue
            for op_name, info in operators.items():
                if not isinstance(info, dict):
                    continue
                if product_outer:
                    country, product = middle_key, outer_key
                else:
                    country, product = outer_key, middle_key
                entries.append(PriceEntry(
                    country=country,
                    operator=op_name,
                    product=product,
                    cost=float(info.get("cost", 999)),
                    count=int(info.get("count", 0)),
                    rate=float(info.get("rate", 0)),
                ))
    entries.sort(key=lambda e: e.sort_key)
    return entries


def query_prices(
    product: str = DEFAULT_PRODUCT,
    country: str = "",
    *,
    proxy: Optional[str] = None,
    proxy_insecure: bool = False,
    timeout: float = 30.0,
) -> List[PriceEntry]:
    """查询 5sim 价格（无需 API key），返回按接码率降序排列的列表。"""
    parts = []
    if product and product != "other":
        parts.append(f"product={product}")
    if country:
        parts.append(f"country={country}")
    qs = "?" + "&".join(parts) if parts else ""
    data = _get(f"/v1/guest/prices{qs}", proxy=proxy, proxy_insecure=proxy_insecure, timeout=timeout)
    return _parse_price_entries(data, product_filter=product, country_filter=country)


def find_buy_candidates(
    product: str = DEFAULT_PRODUCT,
    country: str = "",
    operator: str = DEFAULT_OPERATOR,
    *,
    max_price: Optional[float] = None,
    proxy: Optional[str] = None,
    proxy_insecure: bool = False,
    timeout: float = 30.0,
    limit: int = 8,
    priority: str = "rate",
) -> List[PriceEntry]:
    """Find the best in-stock candidate country/operator pairs for purchase.

    This is used as a fallback when `any/any` (or a partially-specific filter)
    returns `no free phones`.
    """
    entries = query_prices(
        product=product,
        country=country if country and country != "any" else "",
        proxy=proxy,
        proxy_insecure=proxy_insecure,
        timeout=timeout,
    )
    filtered = [e for e in entries if e.product == product and e.count > 0]
    if max_price is not None:
        filtered = [e for e in filtered if e.cost <= max_price]

    if operator and operator != "any":
        preferred = [e for e in filtered if e.operator == operator]
        if preferred:
            filtered = preferred + [e for e in filtered if e.operator != operator]

    unique: List[PriceEntry] = []
    seen: set[tuple[str, str]] = set()
    if priority == "price":
        filtered.sort(key=lambda entry: (entry.cost, -entry.rate, -entry.count))
    for entry in filtered:
        key = (entry.country, entry.operator)
        if key in seen:
            continue
        seen.add(key)
        unique.append(entry)
        if len(unique) >= max(1, limit):
            break
    return unique


def list_countries(*, proxy: Optional[str] = None, proxy_insecure: bool = False, timeout: float = 15.0) -> Dict[str, Any]:
    """获取 5sim 支持的国家列表（无需 API key）。"""
    return _get("/v1/guest/countries", proxy=proxy, proxy_insecure=proxy_insecure, timeout=timeout)


def query_products(country: str, operator: str, *, proxy: Optional[str] = None, proxy_insecure: bool = False, timeout: float = 15.0) -> Dict[str, Any]:
    """查询指定国家/运营商的产品列表（无需 API key）。"""
    return _get(f"/v1/guest/products/{country}/{operator}", proxy=proxy, proxy_insecure=proxy_insecure, timeout=timeout)


# ---------------------------------------------------------------------------
# 购买 / 复用 / 检查 / 取消 / 完成（需鉴权）
# ---------------------------------------------------------------------------


def buy_activation(
    api_key: str,
    country: str,
    operator: str = DEFAULT_OPERATOR,
    product: str = DEFAULT_PRODUCT,
    *,
    max_price: Optional[float] = None,
    enable_reuse: bool = True,
    proxy: Optional[str] = None,
    proxy_insecure: bool = False,
    timeout: float = 30.0,
) -> ActivationOrder:
    """购买激活号码。

    enable_reuse: 向 5sim 传递 reuse=1 以优先分配可复用号码（服务端复用）。
    """
    path = f"/v1/user/buy/activation/{country}/{operator}/{product}"
    params = []
    if max_price is not None and operator == "any":
        params.append(f"maxPrice={max_price}")
    if enable_reuse:
        params.append("reuse=1")
    if params:
        path += "?" + "&".join(params)
    data = _get(path, api_key=api_key, proxy=proxy, proxy_insecure=proxy_insecure, timeout=timeout)
    return ActivationOrder(
        id=int(data.get("id", 0)),
        phone=str(data.get("phone", "")),
        operator=str(data.get("operator", "")),
        product=str(data.get("product", "")),
        price=float(data.get("price", 0)),
        status=str(data.get("status", "")),
        expires=str(data.get("expires", "")),
        sms=data.get("sms") if isinstance(data.get("sms"), list) else None,
        country=str(data.get("country", "")),
        raw=data,
    )


def reuse_number(
    api_key: str,
    phone: str,
    product: str = DEFAULT_PRODUCT,
    *,
    proxy: Optional[str] = None,
    proxy_insecure: bool = False,
    timeout: float = 30.0,
) -> ActivationOrder:
    """复用已有号码。调用 5sim /v1/user/reuse/{product}/{phone}。

    成功返回新的 ActivationOrder（状态 PENDING 或 RECEIVED）。
    """
    path = f"/v1/user/reuse/{product}/{phone}"
    data = _get(path, api_key=api_key, proxy=proxy, proxy_insecure=proxy_insecure, timeout=timeout)
    return ActivationOrder(
        id=int(data.get("id", 0)),
        phone=str(data.get("phone", "")),
        operator=str(data.get("operator", "")),
        product=str(data.get("product", "")),
        price=float(data.get("price", 0)),
        status=str(data.get("status", "")),
        expires=str(data.get("expires", "")),
        sms=data.get("sms") if isinstance(data.get("sms"), list) else None,
        country=str(data.get("country", "")),
        raw=data,
    )


def check_order(api_key: str, order_id: int, *, proxy: Optional[str] = None, proxy_insecure: bool = False, timeout: float = 15.0) -> ActivationOrder:
    """检查订单状态 / 获取短信。"""
    data = _get(f"/v1/user/check/{order_id}", api_key=api_key, proxy=proxy, proxy_insecure=proxy_insecure, timeout=timeout)
    return ActivationOrder(
        id=int(data.get("id", 0)),
        phone=str(data.get("phone", "")),
        operator=str(data.get("operator", "")),
        product=str(data.get("product", "")),
        price=float(data.get("price", 0)),
        status=str(data.get("status", "")),
        expires=str(data.get("expires", "")),
        sms=data.get("sms") if isinstance(data.get("sms"), list) else None,
        country=str(data.get("country", "")),
        raw=data,
    )


def cancel_order(api_key: str, order_id: int, *, proxy: Optional[str] = None, proxy_insecure: bool = False, timeout: float = 15.0) -> ActivationOrder:
    """取消订单。"""
    data = _get(f"/v1/user/cancel/{order_id}", api_key=api_key, proxy=proxy, proxy_insecure=proxy_insecure, timeout=timeout)
    return ActivationOrder(
        id=int(data.get("id", 0)),
        phone=str(data.get("phone", "")),
        operator=str(data.get("operator", "")),
        product=str(data.get("product", "")),
        price=float(data.get("price", 0)),
        status=str(data.get("status", "")),
        expires=str(data.get("expires", "")),
        sms=data.get("sms") if isinstance(data.get("sms"), list) else None,
        country=str(data.get("country", "")),
        raw=data,
    )


def finish_order(api_key: str, order_id: int, *, proxy: Optional[str] = None, proxy_insecure: bool = False, timeout: float = 15.0) -> ActivationOrder:
    """确认完成订单（成功收到短信后调用）。"""
    data = _get(f"/v1/user/finish/{order_id}", api_key=api_key, proxy=proxy, proxy_insecure=proxy_insecure, timeout=timeout)
    return ActivationOrder(
        id=int(data.get("id", 0)),
        phone=str(data.get("phone", "")),
        operator=str(data.get("operator", "")),
        product=str(data.get("product", "")),
        price=float(data.get("price", 0)),
        status=str(data.get("status", "")),
        expires=str(data.get("expires", "")),
        sms=data.get("sms") if isinstance(data.get("sms"), list) else None,
        country=str(data.get("country", "")),
        raw=data,
    )


def ban_order(api_key: str, order_id: int, *, proxy: Optional[str] = None, proxy_insecure: bool = False, timeout: float = 15.0) -> ActivationOrder:
    """封禁订单（号码被拉黑/无法使用时）。"""
    data = _get(f"/v1/user/ban/{order_id}", api_key=api_key, proxy=proxy, proxy_insecure=proxy_insecure, timeout=timeout)
    return ActivationOrder(
        id=int(data.get("id", 0)),
        phone=str(data.get("phone", "")),
        operator=str(data.get("operator", "")),
        product=str(data.get("product", "")),
        price=float(data.get("price", 0)),
        status=str(data.get("status", "")),
        expires=str(data.get("expires", "")),
        sms=data.get("sms") if isinstance(data.get("sms"), list) else None,
        country=str(data.get("country", "")),
        raw=data,
    )


def get_profile(api_key: str, *, proxy: Optional[str] = None, proxy_insecure: bool = False, timeout: float = 15.0) -> Dict[str, Any]:
    """获取账户信息（余额等）。"""
    return _get("/v1/user/profile", api_key=api_key, proxy=proxy, proxy_insecure=proxy_insecure, timeout=timeout)
