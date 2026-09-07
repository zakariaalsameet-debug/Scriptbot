"""
النظام الكامل — نسخة المراقبة الدائمة:
  - يكتشف مينتات بدأت اليوم على Robinhood + Ethereum
  - أي مينت (حتى لو مدفوع حاليًا أو الغاز مرتفع) يُضاف لقائمة مراقبة دائمة
  - يعيد الفحص كل 15 ثانية (سعر من العقد مباشرة + غاز + كمية متبقية)
  - يشتري فور توفر الشرط، ويتوقف عن المراقبة فقط عند: نجاح الشراء،
    انتهاء وقت المرحلة، أو نفاد الكمية
  - لا يشتري نفس المجموعة مرتين أبدًا
  - يرسل إشعار تيليجرام لكل نتيجة نهائية (شراء / انتهاء الفرصة)
"""

import asyncio
import json
import logging
import os
import time
from datetime import datetime, timezone, timedelta

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import websockets
from dotenv import load_dotenv

from buyer import get_web3, attempt_purchase, get_onchain_public_price_wei

load_dotenv()

OPENSEA_API_KEY = os.environ["OPENSEA_API_KEY"]
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
PRIVATE_KEY = os.environ["PRIVATE_KEY"]
WALLET_ADDRESS = os.environ["WALLET_ADDRESS"]
BOT_ENABLED = os.environ.get("BOT_ENABLED", "false").lower() == "true"

ALCHEMY_API_KEY_ROBINHOOD = os.environ["ALCHEMY_API_KEY"]
ALCHEMY_API_KEY_ETHEREUM = os.environ["ALCHEMY_API_KEY_ETHEREUM"]

STREAM_URL = f"wss://stream.openseabeta.com/socket/websocket?token={OPENSEA_API_KEY}&vsn=2.0.0"
TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
DROPS_API_BASE = "https://api.opensea.io/api/v2/drops"

ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"
LOCAL_TZ = timezone(timedelta(hours=3))

HEARTBEAT_INTERVAL = 100
RECV_TIMEOUT = 20
FREE_PRICE_THRESHOLD_USD = 0.01
WATCH_POLL_INTERVAL_SECONDS = 15  # كل كم ثانية نعيد فحص المجموعات المراقَبة
DROP_SCAN_INTERVAL_SECONDS = 60  # كل كم ثانية نعيد اكتشاف الـ Drops الحالية
DROP_LIST_TYPES = ("featured", "upcoming", "recently_minted")
DROP_LIST_PAGE_SIZE = 100

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("auto-buyer")

# HTTP resilience for OpenSea/CoinGecko GET requests. JSON-RPC resilience
# is configured separately in buyer.py.
HTTP_RETRY_TOTAL = int(os.environ.get("HTTP_RETRY_TOTAL", "4"))
HTTP_RETRY_BACKOFF = float(os.environ.get("HTTP_RETRY_BACKOFF", "0.4"))
_http_retry = Retry(
    total=HTTP_RETRY_TOTAL,
    connect=HTTP_RETRY_TOTAL,
    read=HTTP_RETRY_TOTAL,
    status=HTTP_RETRY_TOTAL,
    backoff_factor=HTTP_RETRY_BACKOFF,
    status_forcelist=(429, 500, 502, 503, 504),
    allowed_methods=frozenset({"GET"}),
    raise_on_status=False,
)
HTTP_SESSION = requests.Session()
HTTP_SESSION.mount("https://", HTTPAdapter(max_retries=_http_retry, pool_connections=10, pool_maxsize=10))
HTTP_SESSION.mount("http://", HTTPAdapter(max_retries=_http_retry, pool_connections=10, pool_maxsize=10))
HTTP_TIMEOUT = float(os.environ.get("HTTP_TIMEOUT_SECONDS", "15"))

CHAIN_CONFIGS = {
    "robinhood": {
        "stream_chain_name": "robinhood",
        "rpc_url": f"https://robinhood-mainnet.g.alchemy.com/v2/{ALCHEMY_API_KEY_ROBINHOOD}",
        "max_gas_fee_usd": 0.16,
    },
    "ethereum": {
        "stream_chain_name": "ethereum",
        "rpc_url": f"https://eth-mainnet.g.alchemy.com/v2/{ALCHEMY_API_KEY_ETHEREUM}",
        "max_gas_fee_usd": 0.50,
    },
}

W3_INSTANCES = {key: get_web3(cfg["rpc_url"]) for key, cfg in CHAIN_CONFIGS.items()}
STREAM_NAME_TO_CHAIN_KEY = {cfg["stream_chain_name"]: key for key, cfg in CHAIN_CONFIGS.items()}

buy_lock = asyncio.Lock()

# --- حالة النظام المركزية ---
notified: set[str] = set()        # اشترينا منها بنجاح — ممنوع تتكرر أبدًا
watchlist: dict[str, dict] = {}   # slug -> {"chain_key":..., "detail":...} تحت المراقبة الدائمة
in_flight: set[str] = set()       # قيد المعالجة حاليًا (يمنع تضارب بين اكتشاف جديد ودورة مراقبة)

_eth_price_cache = {"value": None, "ts": 0}


def get_eth_price_usd() -> float:
    now = time.time()
    if _eth_price_cache["value"] and (now - _eth_price_cache["ts"] < 300):
        return _eth_price_cache["value"]
    try:
        resp = HTTP_SESSION.get(
            "https://api.coingecko.com/api/v3/simple/price?ids=ethereum&vs_currencies=usd",
            timeout=HTTP_TIMEOUT,
        )
        price = resp.json()["ethereum"]["usd"]
        _eth_price_cache["value"] = price
        _eth_price_cache["ts"] = now
        return price
    except Exception as e:
        log.warning(f"[السعر] تعذر جلب سعر ETH: {e}")
        return _eth_price_cache["value"] or 3000.0


# ---------------------------------------------------------------------------
# OpenSea
# ---------------------------------------------------------------------------

def fetch_drop_detail(slug: str):
    try:
        resp = HTTP_SESSION.get(
            f"{DROPS_API_BASE}/{slug}",
            headers={"x-api-key": OPENSEA_API_KEY},
            timeout=HTTP_TIMEOUT,
        )
        if resp.status_code == 200:
            return True, resp.json()
        if resp.status_code == 404:
            return False, None
        return None, None
    except Exception as e:
        log.warning(f"[Drops API] خطأ: {e}")
        return None, None


def fetch_drop_page(drop_type: str, chain_key: str, cursor: str | None = None):
    """يجلب صفحة واحدة من قائمة Drops مع دعم cursor pagination."""
    try:
        params = {
            "type": drop_type,
            "limit": DROP_LIST_PAGE_SIZE,
            "chains": CHAIN_CONFIGS[chain_key]["stream_chain_name"],
        }
        if cursor:
            params["cursor"] = cursor

        resp = HTTP_SESSION.get(
            DROPS_API_BASE,
            headers={"x-api-key": OPENSEA_API_KEY},
            params=params,
            timeout=HTTP_TIMEOUT,
        )
        if resp.status_code == 200:
            data = resp.json()
            drops = data.get("drops") or data.get("nft_drops") or []
            return True, drops, data.get("next")
        if resp.status_code in (400, 401, 403, 404, 409, 500):
            log.warning(
                f"[Drops API] فشل جلب القائمة ({drop_type}/{chain_key}) "
                f"HTTP {resp.status_code}: {resp.text[:300]}"
            )
            return False, [], None
        log.warning(f"[Drops API] HTTP {resp.status_code} أثناء جلب القائمة.")
        return False, [], None
    except Exception as e:
        log.warning(f"[Drops API] خطأ في pagination: {e}")
        return False, [], None


def parse_iso(ts: str):
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:
        return None


def started_today_local(stage: dict) -> bool:
    start = parse_iso(stage.get("start_time", ""))
    if not start:
        return False
    return start.astimezone(LOCAL_TZ).date() == datetime.now(LOCAL_TZ).date()


def stage_has_ended(stage: dict) -> bool:
    end = parse_iso(stage.get("end_time", ""))
    if not end:
        return False
    return datetime.now(timezone.utc) > end


def is_free_or_negligible(price_wei: int, eth_price_usd: float) -> bool:
    price_usd = (price_wei / 1e18) * eth_price_usd
    return price_usd < FREE_PRICE_THRESHOLD_USD


async def evaluate_existing_drop(slug: str, chain_key: str):
    """يفحص Drop موجودة مسبقًا، ويضيفها للمراقبة إذا كانت مفتوحة ومجانية."""
    if not slug or slug in notified or slug in watchlist or slug in in_flight:
        return

    in_flight.add(slug)
    try:
        found, detail = await asyncio.to_thread(fetch_drop_detail, slug)
        if not found or not detail or not detail.get("is_minting"):
            return

        stage = detail.get("active_stage")
        if not stage or stage_has_ended(stage):
            return

        contract_address = detail.get("contract_address")
        if not contract_address:
            return

        w3 = W3_INSTANCES[chain_key]
        eth_price_usd = get_eth_price_usd()
        onchain_price = await asyncio.to_thread(
            get_onchain_public_price_wei, w3, contract_address
        )
        price_wei = onchain_price if onchain_price is not None else int(stage.get("price", "0"))

        if not is_free_or_negligible(price_wei, eth_price_usd):
            return

        # المفتوح + المجاني فقط يدخل قائمة المراقبة.
        # المفتاح هو slug، لذلك نفس الـ Drop لا تُضاف مرتين.
        if slug not in watchlist and slug not in notified:
            watchlist[slug] = {"chain_key": chain_key, "detail": detail}
            enqueue_message(build_watching_message(detail, "Drop موجودة ومفتوحة ومجانية — أُضيفت لقائمة المراقبة."))
            log.info(f"👀 '{slug}': Drop حالية مفتوحة ومجانية — أُضيفت لقائمة المراقبة.")

    except Exception as e:
        log.error(f"خطأ غير متوقع بفحص Drop الحالية '{slug}': {e}")
    finally:
        in_flight.discard(slug)


async def scan_existing_drops():
    """يكتشف Drops الحالية عبر OpenSea API مع pagination ويفحص تفاصيل كل Drop."""
    cycle_seen: set[str] = set()
    total_discovered = 0
    total_added = 0

    for chain_key in CHAIN_CONFIGS:
        for drop_type in DROP_LIST_TYPES:
            cursor = None
            page_number = 0

            while True:
                found, drops, next_cursor = await asyncio.to_thread(
                    fetch_drop_page, drop_type, chain_key, cursor
                )
                if not found:
                    break

                page_number += 1
                log.info(
                    f"🔎 فحص Drops الحالية: {chain_key}/{drop_type} — الصفحة {page_number} "
                    f"({len(drops)} Drop)"
                )

                for drop in drops:
                    slug = (drop.get("slug") or drop.get("collection_slug") or "").strip()
                    if not slug or slug in cycle_seen:
                        continue
                    cycle_seen.add(slug)
                    total_discovered += 1

                    before = slug in watchlist
                    await evaluate_existing_drop(slug, chain_key)
                    if not before and slug in watchlist:
                        total_added += 1

                if not next_cursor or next_cursor == cursor:
                    break
                cursor = next_cursor

    log.info(
        f"🔎 اكتمل فحص الـ Drops الحالية: اكتُشف {total_discovered}، "
        f"أُضيف للمراقبة {total_added}."
    )


async def drop_scanner_loop():
    """يشغل اكتشاف الـ Drops الحالية بشكل دوري بالتوازي مع WebSocket والمراقبة."""
    while True:
        try:
            await scan_existing_drops()
        except Exception as e:
            log.error(f"خطأ في ماسح الـ Drops الدورية: {e}")
        await asyncio.sleep(DROP_SCAN_INTERVAL_SECONDS)


# ---------------------------------------------------------------------------
# تيليجرام
# ---------------------------------------------------------------------------

send_queue: "asyncio.Queue[str]" = asyncio.Queue()


def enqueue_message(text: str):
    send_queue.put_nowait(text)


async def telegram_sender():
    while True:
        text = await send_queue.get()
        try:
            await asyncio.to_thread(
                requests.post,
                f"{TELEGRAM_API}/sendMessage",
                data={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"},
                timeout=10,
            )
        except Exception as e:
            log.error(f"خطأ إرسال تليجرام: {e}")
        send_queue.task_done()
        await asyncio.sleep(1.05)


def build_result_message(detail: dict, result: dict, chain_key: str) -> str:
    name = detail.get("collection_name") or detail.get("collection_slug")
    url = detail.get("opensea_url", "")
    chain_label = "Robinhood Chain" if chain_key == "robinhood" else "Ethereum Mainnet"
    return (
        f"✅ <b>تم الشراء بنجاح!</b> ({chain_label})\n\n"
        f"المجموعة: <b>{name}</b>\n"
        f"الكمية: {result['quantity']}\n"
        f"رسوم الغاز: ${result['gas_fee_usd']:.4f}\n"
        f"معاملة: {result['tx_hash']}\n"
        f"🔗 {url}"
    )


def build_watching_message(detail: dict, reason: str) -> str:
    name = detail.get("collection_name") or detail.get("collection_slug")
    return f"👀 <b>تحت المراقبة</b>\n\nالمجموعة: <b>{name}</b>\nالسبب: {reason}\nسنحاول تلقائيًا لحد ما تتوفر الفرصة أو تنتهي."


def build_gaveup_message(detail: dict, reason: str) -> str:
    name = detail.get("collection_name") or detail.get("collection_slug")
    return f"❌ <b>انتهت الفرصة</b>\n\nالمجموعة: <b>{name}</b>\nالسبب: {reason}"


# ---------------------------------------------------------------------------
# محاولة شراء واحدة (تُستخدم بالاكتشاف الأولي وبكل دورة مراقبة)
# ---------------------------------------------------------------------------

async def try_buy_now(slug: str, chain_key: str, detail: dict) -> dict | None:
    """
    يحاول الشراء الآن. يرجع result dict لو حاول فعليًا،
    أو None لو الشروط الأساسية غير محققة أصلاً (مو مجاني بعد، إلخ) — يعني "لسا تحت المراقبة".
    """
    stage = detail.get("active_stage")
    if not stage:
        return None

    max_supply = int(detail.get("max_supply") or 0)
    total_supply = int(detail.get("total_supply") or 0)
    remaining = max_supply - total_supply
    if remaining <= 0:
        return {"success": False, "reason": "sold_out"}

    contract_address = detail.get("contract_address")
    if not contract_address:
        return {"success": False, "reason": "no_contract_address"}

    w3 = W3_INSTANCES[chain_key]
    eth_price_usd = get_eth_price_usd()

    # السعر: نفضّل القراءة المباشرة من العقد (أدق وأسرع من بيانات OpenSea)
    onchain_price = await asyncio.to_thread(get_onchain_public_price_wei, w3, contract_address)
    price_wei = onchain_price if onchain_price is not None else int(stage.get("price", "0"))

    if not is_free_or_negligible(price_wei, eth_price_usd):
        return None  # لسا مدفوع — يبقى بالمراقبة

    max_per_wallet_raw = stage.get("max_total_mintable_by_wallet") or stage.get("max_per_wallet")
    max_per_wallet = int(max_per_wallet_raw) if max_per_wallet_raw is not None else None
    max_gas_fee_usd = CHAIN_CONFIGS[chain_key]["max_gas_fee_usd"]

    async with buy_lock:
        if slug in notified:  # حماية إضافية من التكرار حتى لو صار تزامن
            return {"success": False, "reason": "already_bought"}
        result = await asyncio.to_thread(
            attempt_purchase,
            w3, PRIVATE_KEY, WALLET_ADDRESS,
            contract_address, price_wei, max_per_wallet, remaining,
            eth_price_usd, max_gas_fee_usd,
            slug=slug, opensea_api_key=OPENSEA_API_KEY,
        )
        if result["success"]:
            notified.add(slug)

    return result


# ---------------------------------------------------------------------------
# معالجة أول اكتشاف لمجموعة
# ---------------------------------------------------------------------------

async def evaluate_new_mint(slug: str, chain_key: str):
    if slug in notified or slug in watchlist or slug in in_flight:
        return
    in_flight.add(slug)
    try:
        found, detail = await asyncio.to_thread(fetch_drop_detail, slug)
        if not found or not detail or not detail.get("is_minting"):
            return

        stage = detail.get("active_stage")
        if not stage or not started_today_local(stage):
            return  # فلتر "اليوم فقط" — يبقى صامت زي المتفق عليه سابقًا

        result = await try_buy_now(slug, chain_key, detail)

        if result is None:
            # مو مجاني بعد — نضيفه للمراقبة الدائمة
            watchlist[slug] = {"chain_key": chain_key, "detail": detail}
            enqueue_message(build_watching_message(detail, "السعر الحالي مدفوع — بنراقبه لحد ما يصير مجاني."))
            log.info(f"👀 '{slug}': أُضيف لقائمة المراقبة (مدفوع حاليًا).")
            return

        if result["success"]:
            enqueue_message(build_result_message(detail, result, chain_key))
            log.info(f"✅ '{slug}': تم الشراء عند أول اكتشاف.")
            return

        if result["reason"] == "gas_too_high":
            watchlist[slug] = {"chain_key": chain_key, "detail": detail}
            enqueue_message(build_watching_message(detail, "رسوم الغاز مرتفعة حاليًا — بنراقبه لحد ما تنخفض."))
            log.info(f"👀 '{slug}': أُضيف لقائمة المراقبة (غاز مرتفع).")
            return

        if result["reason"] == "sold_out":
            return  # خلصت الكمية أصلًا، ما يستاهل حتى مراقبة

        if result["reason"] == "balance_too_low":
            enqueue_message(
                f"🔴 <b>تنبيه: الرصيد منخفض جدًا!</b>\n\nالرصيد الحالي: ${result.get('balance_usd', 0):.4f}\n"
                f"النظام قد يفوت فرص شراء حتى تعيد التعبئة."
            )
            watchlist[slug] = {"chain_key": chain_key, "detail": detail}
            return

        # أي سبب آخر (simulation_failed مثلاً) — نراقبه بدل ما نتخلى فورًا
        watchlist[slug] = {"chain_key": chain_key, "detail": detail}
        log.info(f"👀 '{slug}': أُضيف لقائمة المراقبة (سبب: {result['reason']}).")

    except Exception as e:
        log.error(f"خطأ غير متوقع بتقييم '{slug}': {e}")
    finally:
        in_flight.discard(slug)


# ---------------------------------------------------------------------------
# دورة المراقبة الدائمة
# ---------------------------------------------------------------------------

async def watch_loop():
    while True:
        await asyncio.sleep(WATCH_POLL_INTERVAL_SECONDS)
        if not watchlist:
            continue

        for slug in list(watchlist.keys()):
            if slug in in_flight or slug in notified:
                continue
            entry = watchlist.get(slug)
            if not entry:
                continue

            in_flight.add(slug)
            try:
                chain_key = entry["chain_key"]

                found, fresh_detail = await asyncio.to_thread(fetch_drop_detail, slug)
                if not found or not fresh_detail or not fresh_detail.get("is_minting"):
                    watchlist.pop(slug, None)
                    enqueue_message(build_gaveup_message(entry["detail"], "المينت لم يعد نشطًا."))
                    continue

                stage = fresh_detail.get("active_stage")
                if not stage:
                    if fresh_detail.get("next_stage"):
                        # لسا فيه مرحلة قادمة — نستمر بالمراقبة، نحدث البيانات فقط
                        watchlist[slug] = {"chain_key": chain_key, "detail": fresh_detail}
                        continue
                    watchlist.pop(slug, None)
                    enqueue_message(build_gaveup_message(fresh_detail, "لا توجد مرحلة نشطة أو قادمة."))
                    continue

                if stage_has_ended(stage) and not fresh_detail.get("next_stage"):
                    watchlist.pop(slug, None)
                    enqueue_message(build_gaveup_message(fresh_detail, "انتهت المرحلة نهائيًا بدون فرصة شراء مناسبة."))
                    log.info(f"⏱️ '{slug}': انتهى وقت المرحلة — تم إيقاف المراقبة.")
                    continue

                result = await try_buy_now(slug, chain_key, fresh_detail)

                if result is None:
                    watchlist[slug] = {"chain_key": chain_key, "detail": fresh_detail}  # لسا مدفوع، استمر
                    continue

                if result["success"]:
                    watchlist.pop(slug, None)
                    enqueue_message(build_result_message(fresh_detail, result, chain_key))
                    log.info(f"✅ '{slug}': نجح الشراء أثناء المراقبة الدائمة.")
                    continue

                if result["reason"] == "sold_out":
                    watchlist.pop(slug, None)
                    enqueue_message(build_gaveup_message(fresh_detail, "نفدت الكمية قبل ما نشتري."))
                    continue

                # gas_too_high أو أي سبب مؤقت آخر — يبقى بالمراقبة، يعيد المحاولة بالدورة الجاية
                watchlist[slug] = {"chain_key": chain_key, "detail": fresh_detail}

            except Exception as e:
                log.error(f"خطأ بدورة مراقبة '{slug}': {e}")
            finally:
                in_flight.discard(slug)


# ---------------------------------------------------------------------------
# الاتصال بـ OpenSea Stream
# ---------------------------------------------------------------------------

async def listen_opensea():
    msg_ref = 0
    while True:
        try:
            async with websockets.connect(STREAM_URL, ping_interval=None, open_timeout=15) as ws:
                log.info(f"متصل بـ OpenSea Stream — يراقب: {list(CHAIN_CONFIGS.keys())}")
                join_ref = str(msg_ref)
                await ws.send(json.dumps([join_ref, join_ref, "collection:*", "phx_join", {}]))
                msg_ref += 1
                last_heartbeat = time.time()

                while True:
                    if time.time() - last_heartbeat > HEARTBEAT_INTERVAL:
                        hb_ref = str(msg_ref)
                        await ws.send(json.dumps([None, hb_ref, "phoenix", "heartbeat", {}]))
                        msg_ref += 1
                        last_heartbeat = time.time()

                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=RECV_TIMEOUT)
                    except asyncio.TimeoutError:
                        continue

                    try:
                        parsed = json.loads(raw)
                    except json.JSONDecodeError:
                        continue

                    if isinstance(parsed, list) and len(parsed) == 5:
                        _jref, _ref, _topic, event_name, payload_wrapper = parsed
                    else:
                        continue

                    if event_name != "item_transferred":
                        continue

                    payload = (payload_wrapper or {}).get("payload") or {}
                    item = payload.get("item", {}) or {}
                    stream_chain_name = (item.get("chain", {}) or {}).get("name", "")

                    chain_key = STREAM_NAME_TO_CHAIN_KEY.get(stream_chain_name)
                    if chain_key is None:
                        continue

                    from_address = ((payload.get("from_account") or {}).get("address", "") or "").lower()
                    if from_address != ZERO_ADDRESS:
                        continue

                    slug = (payload.get("collection", {}) or {}).get("slug", "")
                    if not slug:
                        continue

                    asyncio.create_task(evaluate_new_mint(slug, chain_key))

        except (websockets.ConnectionClosed, OSError, asyncio.TimeoutError) as e:
            log.warning(f"انقطع الاتصال ({e}). إعادة الاتصال خلال 3 ثوانٍ...")
            await asyncio.sleep(3)
        except Exception as e:
            log.error(f"خطأ غير متوقع: {e}. إعادة المحاولة خلال 5 ثوانٍ...")
            await asyncio.sleep(5)


async def run():
    if not BOT_ENABLED:
        log.warning("🔴 BOT_ENABLED=false — النظام متوقف عمدًا (وضع الأمان).")
        enqueue_message("🔴 البوت شغّال لكن بوضع الإيقاف (BOT_ENABLED=false) — ما رح يشتري لين تفعّله.")
        await telegram_sender()
        return

    enqueue_message(f"✅ نظام الشراء التلقائي (مراقبة دائمة) اشتغل — يراقب: {', '.join(CHAIN_CONFIGS.keys())}")
    await asyncio.gather(listen_opensea(), watch_loop(), drop_scanner_loop(), telegram_sender())


def main():
    backoff = 2
    while True:
        try:
            asyncio.run(run())
        except KeyboardInterrupt:
            log.info("تم الإيقاف يدويًا.")
            break
        except Exception as e:
            log.critical(f"توقف غير متوقع: {e}. إعادة التشغيل خلال {backoff} ثانية...")
            time.sleep(backoff)
            backoff = min(backoff * 2, 30)
            continue
        else:
            break


if __name__ == "__main__":
    main()
