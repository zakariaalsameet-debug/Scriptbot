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

    # معالجة HTTP 429 فقط: نحترم Retry-After إن أرسلته OpenSea،
    # وإلا نستخدم تأخيرًا تدريجيًا حتى لا نكرر الطلب بسرعة ونحصل على 429 متتالٍ.
    max_retries_429 = 5
    retry_delay = 2.0

    for attempt in range(max_retries_429 + 1):
        r = requests.post(
            f"https://api.opensea.io/api/v2/drops/{slug}/mint",
            headers={"x-api-key": api_key, "content-type": "application/json", "accept": "application/json"},
            json={"minter": Web3.to_checksum_address(wallet), "quantity": int(quantity)},
            timeout=15,
        )

        if r.status_code != 429:
            break

        if attempt >= max_retries_429:
            raise RuntimeError(f"OpenSea mint HTTP 429: {r.text[:800]}")

        retry_after = r.headers.get("Retry-After")
        try:
            wait_seconds = max(1.0, float(retry_after)) if retry_after is not None else retry_delay
        except (TypeError, ValueError):
            wait_seconds = retry_delay

        wait_seconds = min(wait_seconds, 60.0)
        log.warning(
            f"[OpenSea 429] Rate limit — إعادة المحاولة بعد {wait_seconds:.1f} ثانية "
            f"({attempt + 1}/{max_retries_429})."
        )
        time.sleep(wait_seconds)
        retry_delay = min(retry_delay * 2.0, 30.0)

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
        # OpenSea email wallets can operate as ERC-4337 smart accounts.
        # Build the same kind of UserOperation instead of trying to send an
        # ordinary EOA transaction from a contract/delegated wallet.
        entry_point = w3.eth.contract(address=ENTRY_POINT_V07, abi=ENTRY_POINT_ABI)
        nonce = int(entry_point.functions.getNonce(checksum_wallet, 0).call())

        # OpenSea's Drops API supplies the exact eligible SeaDrop call (including
        # the correct fee recipient and any stage-specific calldata). This avoids
        # rebuilding stage rules locally.
        api_key = (opensea_api_key or __import__("os").environ.get("OPENSEA_API_KEY", "")).strip()
        if slug and api_key:
            mint = _build_opensea_mint(slug, checksum_wallet, quantity, api_key)
            target = mint["to"]
            inner_call = mint["data"]
            total_value = mint["value"]
            # Execute the API-built call from the delegated smart account.
            smart_account = w3.eth.contract(address=checksum_wallet, abi=SMART_ACCOUNT_ABI)
            call_data = smart_account.functions.execute(
                target,
                total_value,
                inner_call,
            )._encode_transaction_data()
        else:
            seadrop = w3.eth.contract(address=SEADROP_ADDRESS, abi=SEADROP_ABI)
            inner_call = seadrop.functions.mintPublic(
                checksum_contract,
                Web3.to_checksum_address(fee_recipient),
                ZERO_ADDRESS,
                quantity,
            )._encode_transaction_data()
            smart_account = w3.eth.contract(address=checksum_wallet, abi=SMART_ACCOUNT_ABI)
            call_data = smart_account.functions.execute(
                SEADROP_ADDRESS,
                total_value,
                inner_call,
            )._encode_transaction_data()

        init_code = _get_7702_init_code(w3, checksum_wallet)

        gas_price = int(w3.eth.gas_price)
        try:
            priority = int(_rpc_call(w3, "rundler_maxPriorityFeePerGas", []), 16)
            gas_price = max(gas_price, priority)
        except Exception:
            pass

        userop = _build_userop(checksum_wallet, nonce, call_data, gas_price, init_code)

        # Bundler gas estimation. The RPC endpoint used by main.py is also
        # capable of the standard ERC-4337 bundler methods on supported chains.
        estimated = _rpc_call(
            w3,
            "eth_estimateUserOperationGas",
            [userop, ENTRY_POINT_V07],
        )
        userop["callGasLimit"] = _hex(int(estimated["callGasLimit"]))
        userop["verificationGasLimit"] = _hex(int(estimated["verificationGasLimit"]))
        userop["preVerificationGas"] = _hex(int(estimated["preVerificationGas"]))

        # Sign the final UserOperation hash with the wallet's private key.
        userop["signature"] = _sign_userop(private_key, _userop_hash(w3, userop))

        gas_units = (
            int(userop["callGasLimit"], 16)
            + int(userop["verificationGasLimit"], 16)
            + int(userop["preVerificationGas"], 16)
        )
        estimated_gas_fee_usd = (gas_units * gas_price / 1e18) * eth_price_usd
        if estimated_gas_fee_usd > max_gas_fee_usd:
            log.info(f"[تأجيل] رسوم UserOperation ${estimated_gas_fee_usd:.4f} > الحد ${max_gas_fee_usd}.")
            return {"success": False, "reason": "gas_too_high", "gas_fee_usd": estimated_gas_fee_usd}

        wallet_balance_wei = w3.eth.get_balance(checksum_wallet)
        total_cost_wei = total_value + gas_units * gas_price
        if wallet_balance_wei < total_cost_wei:
            log.warning("[إلغاء] الرصيد لا يكفي لتغطية سعر المينت والغاز.")
            return {"success": False, "reason": "insufficient_funds_for_total_cost"}

        submitted_hash = _rpc_call(
            w3,
            "eth_sendUserOperation",
            [userop, ENTRY_POINT_V07],
        )
        log.info(f"[UserOperation مرسلة] {submitted_hash} — كمية: {quantity}")

        receipt = _wait_userop(w3, submitted_hash)
        if receipt:
            if not receipt.get("success", False):
                return {
                    "success": False,
                    "reason": "userop_reverted",
                    "userop_hash": submitted_hash,
                    "error": receipt.get("reason"),
                }
            actual_gas_cost_wei = int(receipt.get("actualGasCost", "0x0"), 16)
            tx_hash = (receipt.get("receipt") or {}).get("transactionHash")
            return {
                "success": True,
                "tx_hash": tx_hash,
                "userop_hash": submitted_hash,
                "quantity": quantity,
                "gas_fee_usd": (actual_gas_cost_wei / 1e18) * eth_price_usd,
                "total_value_wei": total_value,
            }

        return {
            "success": True,
            "tx_hash": None,
            "userop_hash": submitted_hash,
            "quantity": quantity,
            "gas_fee_usd": estimated_gas_fee_usd,
            "total_value_wei": total_value,
            "pending": True,
        }

    except Exception as e:
        error_text = str(e)
        if "0x13da22f2" in error_text.lower():
            log.info("[تأجيل] SeaDrop رفض المعاملة بسبب NotActive — ستتم المحاولة في الدورة القادمة.")
            return {"success": False, "reason": "not_active", "error": error_text}
        log.error(f"[خطأ إرسال] {e}")
        return {"success": False, "reason": "tx_error", "error": error_text}

