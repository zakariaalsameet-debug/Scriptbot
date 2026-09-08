"""
محرك الشراء التلقائي عبر عقد SeaDrop — يدعم أكتر من شبكة (Robinhood + Ethereum).
كل الضوابط الأمنية مركزة هنا بدالة واحدة.
"""

import logging
import time
import requests
from web3 import Web3

log = logging.getLogger("buyer")

SEADROP_ADDRESS = Web3.to_checksum_address("0x00005EA00Ac477B1030CE78506496e8C2dE24bf5")
ZERO_ADDRESS = Web3.to_checksum_address("0x0000000000000000000000000000000000000000")

ENTRY_POINT_V07 = Web3.to_checksum_address("0x0000000071727De22E5E9d8BAf0edAc6f37da032")

ENTRY_POINT_ABI = [
    {
        "inputs": [
            {"name": "sender", "type": "address"},
            {"name": "key", "type": "uint192"},
        ],
        "name": "getNonce",
        "outputs": [{"name": "nonce", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [
            {
                "components": [
                    {"name": "sender", "type": "address"},
                    {"name": "nonce", "type": "uint256"},
                    {"name": "initCode", "type": "bytes"},
                    {"name": "callData", "type": "bytes"},
                    {"name": "accountGasLimits", "type": "bytes32"},
                    {"name": "preVerificationGas", "type": "uint256"},
                    {"name": "gasFees", "type": "bytes32"},
                    {"name": "paymasterAndData", "type": "bytes"},
                    {"name": "signature", "type": "bytes"},
                ],
                "name": "userOp",
                "type": "tuple",
            }
        ],
        "name": "getUserOpHash",
        "outputs": [{"name": "", "type": "bytes32"}],
        "stateMutability": "view",
        "type": "function",
    },
]

SMART_ACCOUNT_ABI = [
    {
        "inputs": [
            {"name": "target", "type": "address"},
            {"name": "value", "type": "uint256"},
            {"name": "data", "type": "bytes"},
        ],
        "name": "execute",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
]

SEADROP_ABI = [
    {
        "inputs": [
            {"name": "nftContract", "type": "address"},
            {"name": "feeRecipient", "type": "address"},
            {"name": "minterIfNotPayer", "type": "address"},
            {"name": "quantity", "type": "uint256"},
        ],
        "name": "mintPublic",
        "outputs": [],
        "stateMutability": "payable",
        "type": "function",
    },
    {
        "inputs": [{"name": "nftContract", "type": "address"}],
        "name": "getAllowedFeeRecipients",
        "outputs": [{"name": "", "type": "address[]"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [{"name": "nftContract", "type": "address"}],
        "name": "getPublicDrop",
        "outputs": [{
            "components": [
                {"name": "mintPrice", "type": "uint80"},
                {"name": "startTime", "type": "uint48"},
                {"name": "endTime", "type": "uint48"},
                {"name": "maxTotalMintableByWallet", "type": "uint16"},
                {"name": "feeBps", "type": "uint16"},
                {"name": "restrictFeeRecipients", "type": "bool"},
            ],
            "name": "",
            "type": "tuple",
        }],
        "stateMutability": "view",
        "type": "function",
    },
]

MIN_BALANCE_RESERVE_USD = 0.10
FEW_THRESHOLD = 20
LIMITED_BUY_QTY = 5
GAS_LIMIT_SAFETY_MARGIN = 1.2


def get_web3(rpc_url: str) -> Web3:
    return Web3(Web3.HTTPProvider(rpc_url))


def get_wallet_balance_usd(w3: Web3, wallet_address: str, eth_price_usd: float) -> float:
    try:
        checksum_wallet = Web3.to_checksum_address(wallet_address)
        balance_wei = w3.eth.get_balance(checksum_wallet)
        return (balance_wei / 1e18) * eth_price_usd
    except Exception as e:
        log.error(f"[الرصيد] تعذر القراءة: {e}")
        return 0.0


def estimate_gas_fee_usd(w3: Web3, eth_price_usd: float, gas_units: int = 150_000) -> float:
    try:
        gas_price_wei = w3.eth.gas_price
        fee_eth = (gas_price_wei * gas_units) / 1e18
        return fee_eth * eth_price_usd
    except Exception as e:
        log.warning(f"[الغاز] تعذر التقدير: {e}")
        return float("inf")


def get_fee_recipient(w3: Web3, nft_contract: str) -> str | None:
    try:
        seadrop = w3.eth.contract(address=SEADROP_ADDRESS, abi=SEADROP_ABI)
        recipients = seadrop.functions.getAllowedFeeRecipients(
            Web3.to_checksum_address(nft_contract)
        ).call()
        if not recipients:
            log.warning(f"[عنوان الرسوم] لا يوجد عنوان مسموح لـ {nft_contract}")
            return None
        return Web3.to_checksum_address(recipients[0])
    except Exception as e:
        log.error(f"[عنوان الرسوم] خطأ استعلام: {e}")
        return None


def decide_quantity(max_per_wallet: int | None, remaining_supply: int) -> int:
    if max_per_wallet is None:
        qty = 1
    elif max_per_wallet <= FEW_THRESHOLD:
        qty = max_per_wallet
    else:
        qty = LIMITED_BUY_QTY
    return max(1, min(qty, remaining_supply))


def get_public_drop_data(w3: Web3, nft_contract: str):
    """قراءة بيانات الـ Public Drop مباشرة من SeaDrop."""
    try:
        seadrop = w3.eth.contract(address=SEADROP_ADDRESS, abi=SEADROP_ABI)
        return seadrop.functions.getPublicDrop(
            Web3.to_checksum_address(nft_contract)
        ).call()
    except Exception as e:
        log.warning(f"[Public Drop] تعذر قراءة بيانات المرحلة العامة: {e}")
        return None


def is_public_drop_active(public_drop) -> bool:
    """يتحقق من أن Public Drop نفسها فعّالة الآن، وليس مجرد وجود Drop في OpenSea."""
    if not public_drop or len(public_drop) < 3:
        return False

    now = int(time.time())
    start_time = int(public_drop[1])
    end_time = int(public_drop[2])

    return start_time <= now <= end_time


def get_onchain_public_price_wei(w3: Web3, nft_contract: str) -> int | None:
    try:
        seadrop = w3.eth.contract(address=SEADROP_ADDRESS, abi=SEADROP_ABI)
        public_drop = seadrop.functions.getPublicDrop(
            Web3.to_checksum_address(nft_contract)
        ).call()
        return int(public_drop[0])  # mintPrice هو أول عنصر بالـ tuple
    except Exception as e:
        log.warning(f"[سعر on-chain] تعذر القراءة، سنعتمد بيانات OpenSea: {e}")
        return None


def _rpc_call(w3: Web3, method: str, params: list) -> object:
    provider = getattr(w3, "provider", None)
    url = getattr(provider, "endpoint_uri", None)
    if not url:
        raise RuntimeError("تعذر معرفة رابط RPC/Bundler من اتصال Web3")
    import requests
    response = requests.post(
        url,
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        timeout=20,
    )
    response.raise_for_status()
    payload = response.json()
    if payload.get("error"):
        raise RuntimeError(f"{method}: {payload['error']}")
    return payload.get("result")


def _hex(value: int) -> str:
    return hex(int(value))


def _build_userop(sender: str, nonce: int, call_data: str, gas_price: int, init_code: str) -> dict:
    # EIP-7702 delegated EOAs must use the 7702 marker in initCode when
    # building the ERC-4337 UserOperation hash/validation path.
    return {
        "sender": Web3.to_checksum_address(sender),
        "nonce": _hex(nonce),
        "initCode": init_code,
        "callData": call_data,
        "callGasLimit": "0x0",
        "verificationGasLimit": "0x0",
        "preVerificationGas": "0x0",
        "maxFeePerGas": _hex(gas_price),
        "maxPriorityFeePerGas": _hex(gas_price),
        "paymasterAndData": "0x",
        # Bundlers ignore this during estimation, but some implementations
        # require a correctly sized signature placeholder.
        "signature": "0x" + ("00" * 65),
    }


def _get_7702_init_code(w3: Web3, wallet: str) -> str:
    code = w3.eth.get_code(Web3.to_checksum_address(wallet))
    raw = code.hex() if hasattr(code, "hex") else str(code)
    raw = raw.lower()
    if raw.startswith("0x"):
        raw = raw[2:]
    # An EIP-7702 delegated EOA exposes: 0xef0100 + 20-byte delegate.
    if raw.startswith("ef0100") and len(raw) >= 46:
        # ERC-4337 uses 0x7702 right-padded to 20 bytes as the initCode marker.
        return "0x7702" + ("00" * 18)
    # A normal deployed smart account does not need the 7702 marker.
    if raw and raw != "00" * len(raw):
        return "0x"
    raise RuntimeError(
        "المحفظة لا تحتوي على عقد Smart Account ولا على تفويض EIP-7702؛ "
        "لا يمكن إرسال UserOperation منها قبل تفعيل Smart Account في OpenSea."
    )


def _userop_hash(w3: Web3, userop: dict) -> bytes:
    ep = w3.eth.contract(address=ENTRY_POINT_V07, abi=ENTRY_POINT_ABI)
    packed = (
        userop["sender"],
        int(userop["nonce"], 16),
        bytes.fromhex(userop["initCode"][2:]),
        bytes.fromhex(userop["callData"][2:]),
        (
            int(userop["verificationGasLimit"], 16) << 128
            | int(userop["callGasLimit"], 16)
        ).to_bytes(32, "big"),
        int(userop["preVerificationGas"], 16),
        (
            int(userop["maxPriorityFeePerGas"], 16) << 128
            | int(userop["maxFeePerGas"], 16)
        ).to_bytes(32, "big"),
        bytes.fromhex(userop["paymasterAndData"][2:]),
        bytes.fromhex(userop["signature"][2:]),
    )
    return bytes(ep.functions.getUserOpHash(packed).call())


def _sign_userop(private_key: str, userop_hash: bytes) -> str:
    # OpenSea/Privy signs the UserOperation hash through personal_sign in the
    # browser flow; keep the same EIP-191 signing convention here.
    from eth_account import Account
    from eth_account.messages import encode_defunct
    signed = Account.sign_message(encode_defunct(hexstr="0x" + userop_hash.hex()), private_key=private_key)
    return signed.signature.hex()


def _wait_userop(w3: Web3, userop_hash: str, timeout_seconds: int = 120):
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        try:
            result = _rpc_call(w3, "eth_getUserOperationReceipt", [userop_hash])
            if result:
                return result
        except Exception:
            pass
        time.sleep(2)
    return None


def _build_opensea_mint(slug: str, wallet: str, quantity: int, api_key: str) -> dict:
    if not slug:
        raise RuntimeError("slug فارغ")
    r = requests.post(
        f"https://api.opensea.io/api/v2/drops/{slug}/mint",
        headers={"x-api-key": api_key, "content-type": "application/json", "accept": "application/json"},
        json={"minter": Web3.to_checksum_address(wallet), "quantity": int(quantity)},
        timeout=15,
    )
    if r.status_code != 200:
        raise RuntimeError(f"OpenSea mint HTTP {r.status_code}: {r.text[:800]}")
    data = r.json()
    target = data.get("to") or data.get("target")
    calldata = data.get("data") or data.get("calldata")
    value = data.get("value")
    if not target or calldata is None or value is None:
        raise RuntimeError(f"OpenSea أعاد بيانات Mint ناقصة: {data}")
    if isinstance(value, str):
        value = int(value, 16) if value.startswith("0x") else int(value)
    return {"to": Web3.to_checksum_address(target), "data": calldata, "value": int(value)}


def _as_int(value, default=0) -> int:
    """Convert common RPC/JSON numeric representations to int."""
    if value is None:
        return default
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        value = value.strip()
        if not value:
            return default
        return int(value, 16) if value.lower().startswith("0x") else int(value)
    return default


def _rpc_personal_sign(w3: Web3, message, wallet: str) -> str:
    """
    Sign exactly the payload returned by prepareSponsoredExecution using the
    JSON-RPC personal_sign method. The wallet/provider, not the bot, owns the
    signing policy.
    """
    if isinstance(message, bytes):
        message = "0x" + message.hex()
    elif not isinstance(message, str):
        raise RuntimeError("prepareSponsoredExecution أعاد رسالة توقيع غير صالحة")
    return str(_rpc_call(w3, "personal_sign", [message, wallet]))


def _extract_prepared_execution(prepared: dict) -> dict:
    """
    Normalize the common response shapes used by sponsored-execution providers.
    No UserOperation is rebuilt here; the provider remains authoritative.
    """
    if not isinstance(prepared, dict):
        raise RuntimeError("prepareSponsoredExecution أعاد استجابة غير صالحة")

    # Some providers wrap the actual execution object.
    execution = (
        prepared.get("execution")
        or prepared.get("sponsoredExecution")
        or prepared.get("userOperation")
        or prepared.get("userOp")
        or prepared
    )
    if not isinstance(execution, dict):
        raise RuntimeError("prepareSponsoredExecution أعاد execution غير صالح")

    signing_message = (
        prepared.get("signingMessage")
        or prepared.get("message")
        or prepared.get("personalSignMessage")
        or execution.get("signingMessage")
        or execution.get("message")
        or execution.get("personalSignMessage")
        or prepared.get("userOpHash")
        or execution.get("userOpHash")
    )

    gas_limit = (
        execution.get("gasLimit")
        or execution.get("totalGasLimit")
        or prepared.get("gasLimit")
        or prepared.get("totalGasLimit")
    )
    max_fee_per_gas = (
        execution.get("maxFeePerGas")
        or prepared.get("maxFeePerGas")
        or execution.get("maxFeePerGasWei")
        or prepared.get("maxFeePerGasWei")
    )

    userop_hash = (
        prepared.get("userOpHash")
        or prepared.get("userOperationHash")
        or execution.get("userOpHash")
        or execution.get("userOperationHash")
    )

    return {
        "execution": execution,
        "signing_message": signing_message,
        "gas_limit": _as_int(gas_limit),
        "max_fee_per_gas": _as_int(max_fee_per_gas),
        "userop_hash": userop_hash,
    }


def _wait_transaction_receipt(w3: Web3, tx_hash: str, timeout_seconds: int = 180):
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        try:
            receipt = w3.eth.get_transaction_receipt(tx_hash)
            if receipt:
                return receipt
        except Exception:
            pass
        time.sleep(2)
    return None


def _receipt_actual_gas_cost_wei(receipt) -> int:
    gas_used = _as_int(receipt.get("gasUsed") if hasattr(receipt, "get") else None)
    effective_gas_price = _as_int(
        receipt.get("effectiveGasPrice") if hasattr(receipt, "get") else None
    )
    if not effective_gas_price and hasattr(receipt, "get"):
        effective_gas_price = _as_int(receipt.get("gasPrice"))
    return gas_used * effective_gas_price


def attempt_purchase(
    w3: Web3,
    private_key: str,
    wallet_address: str,
    nft_contract: str,
    price_wei_per_token: int,
    max_per_wallet: int | None,
    remaining_supply: int,
    eth_price_usd: float,
    max_gas_fee_usd: float,
    slug: str | None = None,
    opensea_api_key: str | None = None,
) -> dict:
    """
    max_gas_fee_usd يُمرَّر من main.py حسب الشبكة (كل شبكة لها حدها الخاص).
    """
    try:
        # تحويل جميع العناوين إلى Checksum Address في بداية العملية لتجنب أي تعارض
        checksum_wallet = Web3.to_checksum_address(wallet_address)
        checksum_contract = Web3.to_checksum_address(nft_contract)
    except Exception as e:
        log.error(f"[العنوان] تنسيق غير صالح: {e}")
        return {"success": False, "reason": "invalid_address", "error": str(e)}

    balance_usd = get_wallet_balance_usd(w3, checksum_wallet, eth_price_usd)
    if balance_usd < MIN_BALANCE_RESERVE_USD:
        log.warning(f"[توقف] الرصيد ${balance_usd:.4f} أقل من الحد ${MIN_BALANCE_RESERVE_USD}.")
        return {"success": False, "reason": "balance_too_low", "balance_usd": balance_usd}

    gas_fee_usd = estimate_gas_fee_usd(w3, eth_price_usd)
    if gas_fee_usd > max_gas_fee_usd:
        log.info(f"[تأجيل] رسوم الغاز ${gas_fee_usd:.4f} > الحد ${max_gas_fee_usd}.")
        return {"success": False, "reason": "gas_too_high", "gas_fee_usd": gas_fee_usd}

    # مهم: OpenSea قد يعرض Drop على أنها نشطة، لكن mintPublic يعتمد على
    # Public Drop المسجلة فعليًا داخل عقد SeaDrop. إذا لم تكن المرحلة العامة
    # نشطة لحظة التنفيذ، يعيد العقد الخطأ NotActive (selector 0x13da22f2).
    public_drop = get_public_drop_data(w3, checksum_contract)
    if not public_drop:
        return {"success": False, "reason": "public_drop_unavailable"}

    if not is_public_drop_active(public_drop):
        log.info(
            f"[تأجيل] Public Drop غير نشطة حاليًا — "
            f"start={int(public_drop[1])}, end={int(public_drop[2])}, now={int(time.time())}."
        )
        return {
            "success": False,
            "reason": "not_active",
            "start_time": int(public_drop[1]),
            "end_time": int(public_drop[2]),
        }

    fee_recipient = get_fee_recipient(w3, checksum_contract)
    if not fee_recipient:
        return {"success": False, "reason": "no_fee_recipient"}

    quantity = decide_quantity(max_per_wallet, remaining_supply)
    total_value = price_wei_per_token * quantity

    try:
        # OpenSea's Drops API supplies the exact eligible SeaDrop call.
        # The slug is intentionally kept as an input to attempt_purchase because
        # main.py discovers and passes it.
        api_key = (opensea_api_key or __import__("os").environ.get("OPENSEA_API_KEY", "")).strip()
        if not slug:
            return {
                "success": False,
                "reason": "slug_required",
                "error": "يجب تمرير slug إلى attempt_purchase لاستخدام مسار OpenSea Email Wallet.",
            }
        if not api_key:
            return {
                "success": False,
                "reason": "opensea_api_key_missing",
                "error": "OPENSEA_API_KEY غير موجود.",
            }

        mint = _build_opensea_mint(slug, checksum_wallet, quantity, api_key)
        target = mint["to"]
        calldata = mint["data"]
        total_value = mint["value"]

        # 1) Simulate the exact transaction returned by OpenSea before asking
        # the sponsored-execution service to prepare it.
        simulation = {
            "from": checksum_wallet,
            "to": target,
            "data": calldata,
            "value": total_value,
        }
        try:
            w3.eth.call(simulation)
        except Exception as e:
            log.info(f"[المحاكاة] معاملة Mint ستفشل: {e}")
            return {
                "success": False,
                "reason": "simulation_failed",
                "error": str(e),
                "quantity": quantity,
                "total_value_wei": total_value,
            }

        # 2) Estimate the actual execution gas for the OpenSea-built call.
        try:
            estimated_call_gas = int(w3.eth.estimate_gas(simulation))
        except Exception as e:
            log.error(f"[تقدير الغاز] تعذر تقدير الغاز: {e}")
            return {"success": False, "reason": "gas_estimation_failed", "error": str(e)}

        gas_price = int(w3.eth.gas_price)
        preliminary_gas_fee_usd = (
            estimated_call_gas * gas_price / 1e18
        ) * eth_price_usd

        # Reject before any sponsored execution is prepared/submitted.
        if preliminary_gas_fee_usd > max_gas_fee_usd:
            log.info(
                f"[تأجيل] الغاز المتوقع ${preliminary_gas_fee_usd:.4f} "
                f"> الحد ${max_gas_fee_usd}."
            )
            return {
                "success": False,
                "reason": "gas_too_high",
                "gas_fee_usd": preliminary_gas_fee_usd,
                "estimated_gas": estimated_call_gas,
            }


        prepare_payload = {
            "from": checksum_wallet,
            "to": target,
            "data": calldata,
            "value": hex(total_value),
            "chainId": hex(int(w3.eth.chain_id)),
            "gasLimit": hex(estimated_call_gas),
        }

        prepared = _rpc_call(
            w3,
            "prepareSponsoredExecution",
            [prepare_payload],
        )
        normalized = _extract_prepared_execution(prepared)

        signing_message = normalized["signing_message"]
        if not signing_message:
            raise RuntimeError(
                "prepareSponsoredExecution لم يُرجع signingMessage/message لـ personal_sign"
            )

        sponsored_gas_limit = normalized["gas_limit"] or estimated_call_gas
        sponsored_max_fee = normalized["max_fee_per_gas"] or gas_price
        sponsored_gas_fee_usd = (
            sponsored_gas_limit * sponsored_max_fee / 1e18
        ) * eth_price_usd

        # 4) Final gas-limit check using the limits returned by the sponsored
        # execution preparation.
        if sponsored_gas_fee_usd > max_gas_fee_usd:
            log.info(
                f"[تأجيل] رسوم Sponsored Execution ${sponsored_gas_fee_usd:.4f} "
                f"> الحد ${max_gas_fee_usd}."
            )
            return {
                "success": False,
                "reason": "gas_too_high",
                "gas_fee_usd": sponsored_gas_fee_usd,
                "estimated_gas": sponsored_gas_limit,
            }

        # 5) Sign exactly what the provider asked for with personal_sign.
        signature = _rpc_personal_sign(
            w3,
            signing_message,
            checksum_wallet,
        )

        # 6) Submit the prepared sponsored execution. The provider supplies the
        # VerifyingPaymaster data and forwards the UserOperation to its Bundler.
        submit_payload = {
            "execution": normalized["execution"],
            "signature": signature,
        }

        submitted = _rpc_call(
            w3,
            "submitSponsoredExecution",
            [submit_payload],
        )

        if isinstance(submitted, dict):
            userop_hash = (
                submitted.get("userOpHash")
                or submitted.get("userOperationHash")
                or normalized["userop_hash"]
            )
            tx_hash = (
                submitted.get("txHash")
                or submitted.get("transactionHash")
            )
        else:
            userop_hash = normalized["userop_hash"]
            tx_hash = submitted if isinstance(submitted, str) else None

        log.info(
            f"[Sponsored Execution مرسلة] "
            f"userOp={userop_hash or 'unknown'} — كمية: {quantity}"
        )

        # 7) Wait for the final transaction receipt. Prefer the transaction hash
        # returned by the sponsored service; otherwise wait for the UserOperation
        # receipt and obtain its underlying transaction hash.
        receipt = None
        if tx_hash:
            receipt = _wait_transaction_receipt(w3, tx_hash)

        if receipt is None and userop_hash:
            userop_receipt = _wait_userop(w3, userop_hash)
            if userop_receipt:
                if not userop_receipt.get("success", False):
                    return {
                        "success": False,
                        "reason": "userop_reverted",
                        "userop_hash": userop_hash,
                        "error": userop_receipt.get("reason"),
                    }
                tx_hash = (
                    (userop_receipt.get("receipt") or {}).get("transactionHash")
                    or tx_hash
                )
                if tx_hash:
                    receipt = _wait_transaction_receipt(w3, tx_hash)

        if receipt is None:
            return {
                "success": False,
                "reason": "receipt_timeout",
                "tx_hash": tx_hash,
                "userop_hash": userop_hash,
                "quantity": quantity,
                "total_value_wei": total_value,
            }

        receipt_status = _as_int(receipt.get("status"), 1)
        if receipt_status != 1:
            return {
                "success": False,
                "reason": "transaction_reverted",
                "tx_hash": tx_hash,
                "userop_hash": userop_hash,
                "quantity": quantity,
            }

        # 8) Calculate the actual gas used from the final receipt, not from the
        # estimate.
        actual_gas_cost_wei = _receipt_actual_gas_cost_wei(receipt)

        return {
            "success": True,
            "tx_hash": tx_hash,
            "userop_hash": userop_hash,
            "quantity": quantity,
            "gas_fee_usd": (actual_gas_cost_wei / 1e18) * eth_price_usd,
            "total_value_wei": total_value,
            "actual_gas_used": _as_int(receipt.get("gasUsed")),
        }

    except Exception as e:
        error_text = str(e)
        if "0x13da22f2" in error_text.lower():
            log.info("[تأجيل] SeaDrop رفض المعاملة بسبب NotActive — ستتم المحاولة في الدورة القادمة.")
            return {"success": False, "reason": "not_active", "error": error_text}
        log.error(f"[خطأ إرسال] {e}")
        return {"success": False, "reason": "tx_error", "error": error_text}

