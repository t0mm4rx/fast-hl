import eth_account
import logging
import secrets

from eth_abi import encode
from eth_account.signers.local import LocalAccount
from eth_utils import keccak, to_hex

from fast_hl.api import API
from fast_hl.info import Info
from fast_hl.utils.constants import MAINNET_API_URL
from fast_hl.utils.signing import (
    ZERO_ADDRESS,
    CancelRequest,
    CancelByCloidRequest,
    ModifyRequest,
    ModifySpec,
    OrderRequest,
    OrderSpec,
    OrderType,
    float_to_usd_int,
    get_timestamp_ms,
    modify_spec_preprocessing,
    modify_spec_to_modify_wire,
    order_grouping_to_number,
    order_request_to_order_spec,
    order_spec_preprocessing,
    order_spec_to_order_wire,
    sign_l1_action,
    sign_usd_transfer_action,
    sign_withdraw_from_bridge_action,
    sign_agent,
    str_to_bytes16,
)
from fast_hl.utils.types import Any, List, Literal, Meta, Optional, Tuple, Cloid


class Exchange(API):

    # Default Max Slippage for Market Orders 5%
    DEFAULT_SLIPPAGE = 0.05

    # async def __new__(cls, *a, **kw):
    #     instance = super().__new__(cls)
    #     await instance.__init__(*a, **kw)
    #     return instance

    async def __init__(
        self,
        wallet: LocalAccount,
        base_url: Optional[str] = None,
        meta: Optional[Meta] = None,
        vault_address: Optional[str] = None,
    ):
        await super().__init__(base_url)
        self.wallet = wallet
        self.vault_address = vault_address
        self.info = await Info(base_url, skip_ws=True)
        if meta is None:
            self.meta = await self.info.meta()
        else:
            self.meta = meta
        self.coin_to_asset = {asset_info["name"]: asset for (asset, asset_info) in enumerate(self.meta["universe"])}

    async def _post_action(self, action, signature, nonce):
        payload = {
            "action": action,
            "nonce": nonce,
            "signature": signature,
            "vaultAddress": self.vault_address,
        }
        logging.debug(payload)
        return await self.post("/exchange", payload)

    def _slippage_price(
        self,
        coin: str,
        is_buy: bool,
        slippage: float,
        px: Optional[float] = None,
    ) -> float:

        if not px:
            # Get midprice
            px = float(self.info.all_mids()[coin])
        # Calculate Slippage
        px *= (1 + slippage) if is_buy else (1 - slippage)
        # We round px to 5 significant figures and 6 decimals
        return round(float(f"{px:.5g}"), 6)

    async def order(
        self,
        coin: str,
        is_buy: bool,
        sz: float,
        limit_px: float,
        order_type: OrderType,
        reduce_only: bool = False,
        cloid: Optional[Cloid] = None,
    ) -> Any:
        order: OrderRequest = {
            "coin": coin,
            "is_buy": is_buy,
            "sz": sz,
            "limit_px": limit_px,
            "order_type": order_type,
            "reduce_only": reduce_only,
        }
        if cloid:
            order["cloid"] = cloid
        return await self.bulk_orders([order])

    async def bulk_orders(self, order_requests: List[OrderRequest]) -> Any:
        order_specs: List[OrderSpec] = [
            order_request_to_order_spec(order, self.coin_to_asset[order["coin"]]) for order in order_requests
        ]

        timestamp = get_timestamp_ms()
        grouping: Literal["na"] = "na"

        has_cloid = False
        for order_spec in order_specs:
            if "cloid" in order_spec["order"] and order_spec["order"]["cloid"]:
                has_cloid = True

        if has_cloid:
            for order_spec in order_specs:
                if "cloid" not in order_spec["order"] or not order_spec["order"]["cloid"]:
                    raise ValueError("all orders must have cloids if at least one has a cloid")

        if has_cloid:
            signature_types = ["(uint32,bool,uint64,uint64,bool,uint8,uint64,bytes16)[]", "uint8"]
        else:
            signature_types = ["(uint32,bool,uint64,uint64,bool,uint8,uint64)[]", "uint8"]

        signature = sign_l1_action(
            self.wallet,
            signature_types,
            [[order_spec_preprocessing(order_spec) for order_spec in order_specs], order_grouping_to_number(grouping)],
            ZERO_ADDRESS if self.vault_address is None else self.vault_address,
            timestamp,
            self.base_url == MAINNET_API_URL,
        )

        return await self._post_action(
            {
                "type": "order",
                "grouping": grouping,
                "orders": [order_spec_to_order_wire(order_spec) for order_spec in order_specs],
            },
            signature,
            timestamp,
        )

    async def modify_order(
        self,
        oid: int,
        coin: str,
        is_buy: bool,
        sz: float,
        limit_px: float,
        order_type: OrderType,
        reduce_only: bool = False,
        cloid: Optional[Cloid] = None,
    ) -> Any:

        modify: ModifyRequest = {
            "oid": oid,
            "order": {
                "coin": coin,
                "is_buy": is_buy,
                "sz": sz,
                "limit_px": limit_px,
                "order_type": order_type,
                "reduce_only": reduce_only,
                "cloid": cloid,
            },
        }
        return await self.bulk_modify_orders([modify])

    async def bulk_modify_orders(self, modify_requests: List[ModifyRequest]) -> Any:
        modify_specs: List[ModifySpec] = [
            {
                "oid": modify["oid"],
                "order": order_request_to_order_spec(modify["order"], self.coin_to_asset[modify["order"]["coin"]]),
                "orderType": modify["order"]["order_type"],
            }
            for modify in modify_requests
        ]

        timestamp = get_timestamp_ms()

        signature_types = ["(uint64,uint32,bool,uint64,uint64,bool,uint8,uint64,bytes16)[]"]

        signature = sign_l1_action(
            self.wallet,
            signature_types,
            [[modify_spec_preprocessing(modify_spec) for modify_spec in modify_specs]],
            ZERO_ADDRESS if self.vault_address is None else self.vault_address,
            timestamp,
            self.base_url == MAINNET_API_URL,
            action_type_code=40,
        )

        return await self._post_action(
            {
                "type": "batchModify",
                "modifies": [modify_spec_to_modify_wire(modify_spec) for modify_spec in modify_specs],
            },
            signature,
            timestamp,
        )

    async def market_open(
        self,
        coin: str,
        is_buy: bool,
        sz: float,
        px: Optional[float] = None,
        slippage: float = DEFAULT_SLIPPAGE,
        cloid: Optional[Cloid] = None,
    ) -> Any:

        # Get aggressive Market Price
        px = self._slippage_price(coin, is_buy, slippage, px)
        # Market Order is an aggressive Limit Order IoC
        return await self.order(coin, is_buy, sz, px, order_type={"limit": {"tif": "Ioc"}}, reduce_only=False, cloid=cloid)

    async def market_close(
        self,
        coin: str,
        sz: Optional[float] = None,
        px: Optional[float] = None,
        slippage: float = DEFAULT_SLIPPAGE,
        cloid: Optional[Cloid] = None,
    ) -> Any:
        positions = self.info.user_state(self.wallet.address)["assetPositions"]
        for position in positions:
            item = position["position"]
            if coin != item["coin"]:
                continue
            szi = float(item["szi"])
            if not sz:
                sz = szi
            is_buy = True if szi < 0 else False
            # Get aggressive Market Price
            px = self._slippage_price(coin, is_buy, slippage, px)
            # Market Order is an aggressive Limit Order IoC
            return await self.order(coin, is_buy, sz, px, order_type={"limit": {"tif": "Ioc"}}, reduce_only=True, cloid=cloid)

    async def cancel(self, coin: str, oid: int) -> Any:
        return await self.bulk_cancel([{"coin": coin, "oid": oid}])

    async def cancel_by_cloid(self, coin: str, cloid: Cloid) -> Any:
        return await self.bulk_cancel_by_cloid([{"coin": coin, "cloid": cloid}])

    async def bulk_cancel(self, cancel_requests: List[CancelRequest]) -> Any:
        timestamp = get_timestamp_ms()
        signature = sign_l1_action(
            self.wallet,
            ["(uint32,uint64)[]"],
            [[(self.coin_to_asset[cancel["coin"]], cancel["oid"]) for cancel in cancel_requests]],
            ZERO_ADDRESS if self.vault_address is None else self.vault_address,
            timestamp,
            self.base_url == MAINNET_API_URL,
        )
        return await self._post_action(
            {
                "type": "cancel",
                "cancels": [
                    {
                        "asset": self.coin_to_asset[cancel["coin"]],
                        "oid": cancel["oid"],
                    }
                    for cancel in cancel_requests
                ],
            },
            signature,
            timestamp,
        )

    async def bulk_cancel_by_cloid(self, cancel_requests: List[CancelByCloidRequest]) -> Any:
        timestamp = get_timestamp_ms()
        signature = sign_l1_action(
            self.wallet,
            ["(uint32,bytes16)[]"],
            [
                [
                    (self.coin_to_asset[cancel["coin"]], str_to_bytes16(cancel["cloid"].to_raw()))
                    for cancel in cancel_requests
                ]
            ],
            ZERO_ADDRESS if self.vault_address is None else self.vault_address,
            timestamp,
            self.base_url == MAINNET_API_URL,
        )
        return await self._post_action(
            {
                "type": "cancelByCloid",
                "cancels": [
                    {
                        "asset": self.coin_to_asset[cancel["coin"]],
                        "cloid": cancel["cloid"].to_raw(),
                    }
                    for cancel in cancel_requests
                ],
            },
            signature,
            timestamp,
        )

    async def update_leverage(self, leverage: int, coin: str, is_cross: bool = True) -> Any:
        timestamp = get_timestamp_ms()
        asset = self.coin_to_asset[coin]
        signature = sign_l1_action(
            self.wallet,
            ["uint32", "bool", "uint32"],
            [asset, is_cross, leverage],
            ZERO_ADDRESS if self.vault_address is None else self.vault_address,
            timestamp,
            self.base_url == MAINNET_API_URL,
        )
        return await self._post_action(
            {
                "type": "updateLeverage",
                "asset": asset,
                "isCross": is_cross,
                "leverage": leverage,
            },
            signature,
            timestamp,
        )

    async def update_isolated_margin(self, amount: float, coin: str) -> Any:
        timestamp = get_timestamp_ms()
        asset = self.coin_to_asset[coin]
        amount = float_to_usd_int(amount)
        signature = sign_l1_action(
            self.wallet,
            ["uint32", "bool", "int64"],
            [asset, True, amount],
            ZERO_ADDRESS if self.vault_address is None else self.vault_address,
            timestamp,
            self.base_url == MAINNET_API_URL,
        )
        return await self._post_action(
            {
                "type": "updateIsolatedMargin",
                "asset": asset,
                "isBuy": True,
                "ntli": amount,
            },
            signature,
            timestamp,
        )

    async def usd_transfer(self, amount: float, destination: str) -> Any:
        timestamp = get_timestamp_ms()
        payload = {
            "destination": destination,
            "amount": str(amount),
            "time": timestamp,
        }
        is_mainnet = self.base_url == MAINNET_API_URL
        signature = sign_usd_transfer_action(self.wallet, payload, is_mainnet)
        return await self._post_action(
            {
                "chain": "Arbitrum" if is_mainnet else "ArbitrumTestnet",
                "payload": payload,
                "type": "usdTransfer",
            },
            signature,
            timestamp,
        )

    async def withdraw_from_bridge(self, usd: float, destination: str) -> Any:
        timestamp = get_timestamp_ms()
        payload = {
            "destination": destination,
            "usd": str(usd),
            "time": timestamp,
        }
        is_mainnet = self.base_url == MAINNET_API_URL
        signature = sign_withdraw_from_bridge_action(self.wallet, payload, is_mainnet)
        return await self._post_action(
            {
                "chain": "Arbitrum" if is_mainnet else "ArbitrumTestnet",
                "payload": payload,
                "type": "withdraw2",
            },
            signature,
            timestamp,
        )

    async def approve_agent(self, name: Optional[str] = None) -> Tuple[Any, str]:
        agent_key = "0x" + secrets.token_hex(32)
        account = eth_account.Account.from_key(agent_key)
        if name is not None:
            connection_id = keccak(encode(["address", "string"], [account.address, name]))
        else:
            connection_id = keccak(encode(["address"], [account.address]))
        agent = {
            "source": "https://hyperliquid.xyz",
            "connectionId": connection_id,
        }
        timestamp = get_timestamp_ms()
        is_mainnet = self.base_url == MAINNET_API_URL
        signature = sign_agent(self.wallet, agent, is_mainnet)
        agent["connectionId"] = to_hex(agent["connectionId"])
        action = {
            "chain": "Arbitrum" if is_mainnet else "ArbitrumTestnet",
            "agent": agent,
            "agentAddress": account.address,
            "type": "connect",
        }
        if name is not None:
            action["extraAgentName"] = name
        return (
            await self._post_action(
                action,
                signature,
                timestamp,
            ),
            agent_key,
        )
