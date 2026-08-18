import sys
import os
# Lấy path tới root của project
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../"))
sys.path.append(PROJECT_ROOT)

import sys, logging
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
sys.stderr.reconfigure(encoding='utf-8', errors='replace')

import asyncio
import websockets
import json
import aiohttp
from logging.handlers import RotatingFileHandler
import signal
from typing import Any, Dict, Tuple, Optional
from config import CHAIN_ID_MAP, NPM_ADDRESSES, FACTORY_ADDRESSES, MASTERCHEF_ADDRESSES, CHAIN_API_MAP
from services.list_farm_pancake import get_total_alloc_point_each_chain, get_total_cake_per_day_on_chain, get_web3, get_abi, get_contract, get_nft_ids_by_all_status, process_nft_mint_data_evm
from services.execute_data import insert_nft_data
from services.update_query import process_batch_nft_summary
from app import save_update_status

DISCORD_WEBHOOK_URL = "https://discordapp.com/api/webhooks/1377961748925124681/4L4i0oxq6PD1jLlBUV2IxH-G2vobb-ESm2VhKWL30dQztF4sRVg8IkgOoWe4W2EB0IFS"
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
ZERO_ADDRESS_TOPIC = "0x" + "0" * 64
WATCH_WALLETS = [
    "0x88DE2AB47352779494547CaCCB31eE1A133dd334",
    "0x349F8F068120E04B359556E442A579Af41ebF486",
    "0x065994BeC6cA97AeF488f76824580814Be4E024F",
    "0x9b73E95909Be63F02b06130716384c3030C74D8D",
    "0x89B8274BbC46A0db474E3Df381688F80DfFccB6b"
]

# Các chain EVM (WSS + Explorer)
EVM_CHAINS = {
    "BNB": {
        "wss": "wss://bsc-mainnet.infura.io/ws/v3/afb06acf1c3542aca75c89203c9f9a28",
        "explorer": "https://bscscan.com/tx/"
    },
    "ETH": {
        "wss": "wss://mainnet.infura.io/ws/v3/afb06acf1c3542aca75c89203c9f9a28",
        "explorer": "https://etherscan.io/tx/"
    },
    "ARB": {
        "wss": "wss://arbitrum-mainnet.infura.io/ws/v3/afb06acf1c3542aca75c89203c9f9a28",
        "explorer": "https://arbiscan.io/tx/"
    },
    "BAS": {
        "wss": "wss://base-mainnet.infura.io/ws/v3/afb06acf1c3542aca75c89203c9f9a28",
        "explorer": "https://basescan.org/tx/"
    },
    "LIN": {
        "wss": "wss://linea-mainnet.infura.io/ws/v3/afb06acf1c3542aca75c89203c9f9a28",
        "explorer": "https://lineascan.build/tx/"
    }
}

NPM_ADDRESSES = {
    'ETH': "0x46A15B0b27311cedF172AB29E4f4766fbE7F4364",
    'BNB': "0x46A15B0b27311cedF172AB29E4f4766fbE7F4364",
    'BAS': "0x46A15B0b27311cedF172AB29E4f4766fbE7F4364",
    'ARB': "0x46A15B0b27311cedF172AB29E4f4766fbE7F4364",
    'LIN': "0x46A15B0b27311cedF172AB29E4f4766fbE7F4364",
}

# ---------------- CONFIG ----------------
MAX_WORKERS = 3  # number of background workers to process NFT events
NFT_QUEUE_MAXSIZE = 1000
DISCORD_CONCURRENT_SEMAPHORE = 4  # limit concurrent discord posts
WEBSOCKET_PING_INTERVAL = 20
WEBSOCKET_PING_TIMEOUT = 30
RECONNECT_DELAY = 5

# ---------------- LOGGING ----------------
log_file = "logs/wss_evm_monitor.log"
# log_file = "wss_evm_monitor.log"
handler = RotatingFileHandler(log_file, maxBytes=5_000_000, backupCount=3, encoding='utf-8')
stream_handler = logging.StreamHandler(sys.stdout)

formatter = logging.Formatter(
    "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
handler.setFormatter(formatter)
stream_handler.setFormatter(formatter)

logging.basicConfig(
    level=logging.INFO,
    handlers=[handler, stream_handler],
)
logger = logging.getLogger("EVM_Listener")

# ---------------- GLOBALS / PLACEHOLDERS ----------------
# The following dictionaries/constants are expected to be provided by your environment.
EVM_CHAINS: Dict[str, Dict[str, Any]] = globals().get("EVM_CHAINS", {})
WATCH_WALLETS = globals().get("WATCH_WALLETS", [])
NPM_ADDRESSES = globals().get("NPM_ADDRESSES", {})
FACTORY_ADDRESSES = globals().get("FACTORY_ADDRESSES", {})
MASTERCHEF_ADDRESSES = globals().get("MASTERCHEF_ADDRESSES", {})
DISCORD_WEBHOOK_URL = globals().get("DISCORD_WEBHOOK_URL", None)
TRANSFER_TOPIC = globals().get("TRANSFER_TOPIC", "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef")  # first bytes of Transfer topic

# ---------------- RESOURCE MANAGERS ----------------
class ChainContext:
    """Cache Web3 + contract objects per chain to avoid re-instantiation."""

    def __init__(self):
        self._cache: Dict[str, Dict[str, Any]] = {}

    def get(self, chain_name: str) -> Dict[str, Any]:
        if chain_name in self._cache:
            return self._cache[chain_name]

        w3 = get_web3(chain_name)
        npm_address = NPM_ADDRESSES.get(chain_name)
        factory_address = FACTORY_ADDRESSES.get(chain_name)
        masterchef_address = MASTERCHEF_ADDRESSES.get(chain_name)

        ctx = {
            "w3": w3,
            "npm": get_contract(w3, npm_address, get_abi(chain_name, npm_address)) if npm_address else None,
            "factory": get_contract(w3, factory_address, get_abi(chain_name, factory_address)) if factory_address else None,
            "masterchef": get_contract(w3, masterchef_address, get_abi(chain_name, masterchef_address)) if masterchef_address else None,
        }
        self._cache[chain_name] = ctx
        return ctx

CHAIN_CTX = ChainContext()

# ---------------- DISCORD SENDER ----------------
class DiscordSender:
    def __init__(self, webhook_url: Optional[str]):
        self.webhook_url = webhook_url
        self._session: Optional[aiohttp.ClientSession] = None
        self._sema = asyncio.Semaphore(DISCORD_CONCURRENT_SEMAPHORE)

    async def start(self):
        if self.webhook_url and (self._session is None or self._session.closed):
            self._session = aiohttp.ClientSession()

    async def stop(self):
        if self._session and not self._session.closed:
            await self._session.close()

    async def send(self, chain, tx_hash, from_addr, to_addr, token_id):
        if not self.webhook_url:
            return
        await self.start()
        msg = f"🧾 **NFT MINT DETECTED**\n🌐 **Chain:** {chain}\n🔹 **From:** `{from_addr}`\n🔹 **To:** `{to_addr}`\n🔹 **TokenID:** `{token_id}`\n🔗 {EVM_CHAINS.get(chain, {}).get('explorer', '')}{tx_hash}"
        async with self._sema:
            try:
                async with self._session.post(self.webhook_url, json={"content": msg}) as resp:
                    if resp.status not in (200, 204):
                        logger.warning("[%s] Discord webhook returned %s", chain, resp.status)
            except Exception as e:
                logger.exception("[%s] Failed to send Discord message: %s", chain, e)

DISCORD = DiscordSender(DISCORD_WEBHOOK_URL)

# ---------------- WORKER QUEUE ----------------
NFT_QUEUE: asyncio.Queue = asyncio.Queue(maxsize=NFT_QUEUE_MAXSIZE)

async def nft_worker(worker_id: int):
    logger.info("Worker-%d started", worker_id)
    while True:
        try:
            chain_name, token_id, to_addr, tx_hash, from_addr = await NFT_QUEUE.get()
            logger.debug("Worker-%d processing %s %s", worker_id, chain_name, token_id)

            # Use cached chain context
            ctx = CHAIN_CTX.get(chain_name)
            w3 = ctx["w3"]
            npm_contract = ctx.get("npm")
            factory_contract = ctx.get("factory")
            masterchef_contract = ctx.get("masterchef")

            multiplier_chain = get_total_alloc_point_each_chain(chain=chain_name)
            total_cake_per_day = get_total_cake_per_day_on_chain(chain_name)
            cake_per_second = total_cake_per_day / 86400 if total_cake_per_day else 0

            try:
                active_ids, inactive_ids, unknown_ids, status_map, position_map = get_nft_ids_by_all_status(
                    w3, chain_name, CHAIN_API_MAP.get(chain_name), [token_id], npm_contract, factory_contract
                )
                logger.info("[%s] Worker-%d NFT %s status_map: %s position_map: %s inactive_ids: %s", chain_name, worker_id, token_id, status_map, position_map, inactive_ids)

                result = process_nft_mint_data_evm(
                    chain_name, to_addr, token_id, status_map, position_map, factory_contract,
                    w3, CHAIN_API_MAP.get(chain_name), multiplier_chain, cake_per_second, npm_contract, masterchef_contract,
                    inactive_ids, get_abi(chain_name, NPM_ADDRESSES.get(chain_name)), get_abi(chain_name, MASTERCHEF_ADDRESSES.get(chain_name)), mode="realtime"
                )

                if result:
                    new_pos_ids = insert_nft_data([result])
                    process_batch_nft_summary([result], list_ids=new_pos_ids)
                    save_update_status([to_addr], [chain_name])
                    logger.info("[%s] ✅ Worker-%d inserted NFT %s for wallet %s (id: %s)", chain_name, worker_id, token_id, to_addr, new_pos_ids[0])
                    logger.info(f"Result for NFT {token_id} on {chain_name}: {result}")

            except Exception:
                logger.exception("[%s] Worker-%d error processing token %s", chain_name, worker_id, token_id)

        except asyncio.CancelledError:
            logger.info("Worker-%d cancelled, exiting", worker_id)
            break
        except Exception:
            logger.exception("Worker-%d encountered unexpected error", worker_id)
        finally:
            try:
                NFT_QUEUE.task_done()
            except Exception:
                pass

# ---------------- LISTENER ----------------
async def listen_chain(chain_name: str, ws_url: str, nft_address: str, wallet_topics: list):
    """Connect to a chain websocket and push events into NFT_QUEUE."""
    sub_payload = lambda topic: json.dumps({
        "jsonrpc": "2.0",
        "id": topic,
        "method": "eth_subscribe",
        "params": [
            "logs",
            {"address": nft_address, "topics": [TRANSFER_TOPIC, ZERO_ADDRESS_TOPIC, topic]},
        ],
    })

    while True:
        try:
            async with websockets.connect(ws_url, ping_interval=WEBSOCKET_PING_INTERVAL, ping_timeout=WEBSOCKET_PING_TIMEOUT) as ws:
                logger.info("[%s] ✅ Connected to WebSocket", chain_name)

                for wallet_topic in wallet_topics:
                    await ws.send(sub_payload(wallet_topic))
                    logger.info("[%s] 📡 Subscribed for wallet topic %s", chain_name, wallet_topic[-6:])

                async for raw_msg in ws:
                    try:
                        data = json.loads(raw_msg)
                    except Exception:
                        logger.debug("[%s] Received non-json message", chain_name)
                        continue

                    if data.get("method") != "eth_subscription":
                        continue

                    log = data["params"]["result"]
                    tx_hash = log.get("transactionHash")
                    topics = log.get("topics", [])

                    if len(topics) < 4:
                        logger.debug("[%s] Skipping log with insufficient topics", chain_name)
                        continue

                    # Parse addresses and token id
                    try:
                        from_addr = "0x" + topics[1][-40:]
                        to_addr = "0x" + topics[2][-40:]
                        token_id = int(topics[3], 16)
                    except Exception:
                        logger.exception("[%s] Failed to parse topics: %s", chain_name, topics)
                        continue

                    logger.info("[%s] 🔔 New NFT Mint → %s | TokenID %s", chain_name, to_addr, token_id)

                    # Send discord but don't block processing
                    asyncio.create_task(DISCORD.send(chain_name, tx_hash, from_addr, to_addr, token_id))

                    # Push to worker queue (drop if queue full to avoid memory blowup)
                    try:
                        NFT_QUEUE.put_nowait((chain_name, token_id, to_addr, tx_hash, from_addr))
                    except asyncio.QueueFull:
                        logger.warning("[%s] NFT_QUEUE full, dropping event %s", chain_name, token_id)

        except Exception as e:
            logger.warning("[%s] ⚠️ Listener error: %s. Reconnecting in %ss...", chain_name, e, RECONNECT_DELAY)
            await asyncio.sleep(RECONNECT_DELAY)

# ---------------- START / SHUTDOWN ----------------
class Runner:
    def __init__(self):
        self._tasks = []
        self._worker_tasks = []
        self._stop = asyncio.Event()

    async def start(self):
        # Start workers
        for i in range(MAX_WORKERS):
            t = asyncio.create_task(nft_worker(i))
            self._worker_tasks.append(t)

        # Build wallet topics
        wallet_topics = ["0x" + "0" * 24 + w[2:].lower() for w in WATCH_WALLETS]

        # Start listeners
        for name, conf in EVM_CHAINS.items():
            nft_addr = NPM_ADDRESSES.get(name, "").lower()
            ws_url = conf.get("wss")
            if not ws_url or not nft_addr:
                logger.warning("Skipping chain %s: missing ws url or nft addr", name)
                continue
            t = asyncio.create_task(listen_chain(name, ws_url, nft_addr, wallet_topics))
            self._tasks.append(t)

        # Start discord session
        await DISCORD.start()

        # Wait until stop
        await self._stop.wait()
        logger.info("Runner stopping...")

    async def stop(self):
        # Signal stop
        self._stop.set()

        # Cancel listener tasks
        for t in self._tasks:
            t.cancel()
        # Cancel worker tasks
        for t in self._worker_tasks:
            t.cancel()

        # Wait for cancellation
        await asyncio.gather(*self._tasks, return_exceptions=True)
        await asyncio.gather(*self._worker_tasks, return_exceptions=True)

        # Drain queue (optional): attempt to process remaining items
        try:
            while not NFT_QUEUE.empty():
                await asyncio.sleep(0.1)
        except Exception:
            pass

        # Close discord session
        await DISCORD.stop()


RUNNER = Runner()

def _signal_handler(signame):
    logger.info("Received signal %s, shutting down...", signame)
    asyncio.create_task(RUNNER.stop())

async def _main():
    loop = asyncio.get_running_loop()
    for signame in ("SIGINT", "SIGTERM"):
        try:
            loop.add_signal_handler(getattr(signal, signame), lambda s=signame: _signal_handler(s))
        except NotImplementedError:
            # Windows event loop may not support add_signal_handler
            pass

    await RUNNER.start()

if __name__ == "__main__":
    try:
        print("Starting EVM NFT Mint Monitor...")
        asyncio.run(_main())
    except Exception:
        logger.exception("Main exited with exception")