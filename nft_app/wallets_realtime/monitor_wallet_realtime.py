import sys
import os
# Lấy path tới root của project
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../"))
sys.path.append(PROJECT_ROOT)

# wss_monitor_service.py
import asyncio
import json
import base64
import struct
import time
import logging
from typing import List, Optional, Callable, Dict, Any
import websockets
from solders.pubkey import Pubkey
from app import save_update_status

# --- IMPORT YOUR EXISTING FUNCTIONS ---
# Make sure these functions are importable and are thread-safe (or run in thread)
from config import CLIENTS_SOL_ENDPOINTS
from services.solana.get_wallet_info import get_all_status_nft_ids_sol, process_nft_mint_data_sol, send_discord_webhook_message
from services.execute_data import insert_nft_data
from services.update_query import process_batch_nft_summary

# --- CONFIG ---
# WS_URL = "wss://shy-spring-card.solana-mainnet.quiknode.pro/6a97979ed162924bd71e878f5517215efab54766"
WS_URL = "wss://hardworking-dark-telescope.solana-mainnet.quiknode.pro/547aae6632a0960cd2c3d93cffed3ab52d15d4a1/"
SPL_TOKEN_PROGRAM = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
TOKEN_ACCOUNT_SIZE = 170  # change to 170 if needed
WALLETS_ADDRESS = [
    "4rDyyA4vydw4T5uekxY5La4Ywv43nSZ2PgG7rfBfvQAJ",
    "CJoUCt78FNbJJcKW3CnmLG9CVq6ANSTiXWV1tyN5dXw9",
    "DGHsf8b99KyWPErCbVuXcPUxAXwaC7bqndPgEVvmSAFn",
    "8x4zj74myKzox48jUMHskfNo4NHuAzXeLyXs7HLUSYzL",
    "HJncdQqZwAjD5sCTP2dxqqxzSF1XQrFdXwPYJgAj1dma"
]

# Runtime tuning
MAX_CONCURRENT_TASKS = 4         # how many NFT analyze tasks run in parallel
PROCESS_TIMEOUT = 60             # seconds to wait for single processing (thread)
ACK_TIMEOUT = 10                 # seconds to wait for subscription ACKs
RECONNECT_BASE = 1.0             # seconds initial backoff
RECONNECT_MAX = 30.0             # seconds max backoff
DEDUPE_TTL = 2 * 60 * 60         # dedupe TTL (2 hours) — matches cron interval

# # Optional: use Redis for dedupe (recommended in production)
# USE_REDIS = False
# REDIS_URL = "redis://localhost:6379/0"

# --- logging ---
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", filename="logs/wss_sol_monitor.log", filemode="a")
log = logging.getLogger("wss-monitor")

# --- small helper: parse token account base64 -> (mint, owner, amount) ---
def parse_token_account(data_b64: str):
    try:
        raw = base64.b64decode(data_b64)
        mint = str(Pubkey.from_bytes(raw[0:32]))
        owner = str(Pubkey.from_bytes(raw[32:64]))
        amount = struct.unpack_from("<Q", raw, 64)[0]
        return mint, owner, amount
    except Exception as e:
        log.warning("Failed to parse token account: %s", e)
        return None, None, 0

# --- dedupe store (in-memory with TTL) ---
class SeenStore:
    def __init__(self):
        self._store: Dict[str, float] = {}
        self._lock = asyncio.Lock()

    async def seen(self, key: str) -> bool:
        async with self._lock:
            self._cleanup_locked()
            return key in self._store

    async def mark(self, key: str):
        async with self._lock:
            self._store[key] = time.time() + DEDUPE_TTL

    def _cleanup_locked(self):
        now = time.time()
        to_del = [k for k, exp in self._store.items() if exp <= now]
        for k in to_del:
            del self._store[k]

# --- main service class ---
class NFTWSSMonitor:
    def __init__(
        self,
        wallets: List[str],
        ws_url: str = WS_URL,
        token_program: str = SPL_TOKEN_PROGRAM,
        token_account_size: int = TOKEN_ACCOUNT_SIZE,
        clients_endpoints = CLIENTS_SOL_ENDPOINTS,
        on_error: Optional[Callable[[Exception], None]] = None
    ):
        self.wallets = wallets
        self.ws_url = ws_url
        self.token_program = token_program
        self.token_account_size = token_account_size
        self.clients_endpoints = clients_endpoints
        self.on_error = on_error
        self.seen = SeenStore()
        self.sem = asyncio.Semaphore(MAX_CONCURRENT_TASKS)
        self._stop = asyncio.Event()

    async def start(self):
        backoff = RECONNECT_BASE
        while not self._stop.is_set():
            try:
                await self._run_once()
                # if _run_once returns normally, reset backoff
                backoff = RECONNECT_BASE
            except asyncio.CancelledError:
                log.info("Cancelled")
                break
            except Exception as e:
                log.exception("WSS monitor error: %s", e)
                if self.on_error:
                    self.on_error(e)
                log.info("Reconnect after %.1fs", backoff)
                await asyncio.sleep(backoff)
                backoff = min(RECONNECT_MAX, backoff * 2)

    async def stop(self):
        self._stop.set()

    async def _run_once(self):
        log.info("Connecting to WSS %s", self.ws_url)
        async with websockets.connect(self.ws_url, ping_interval=20, max_size=None) as ws:
            sub_map = {}
            # subscribe
            await self._subscribe_all(ws, sub_map)
            log.info("Subscribed %d wallets; listening...", len(sub_map))

            # listen loop
            async for raw in ws:
                if self._stop.is_set():
                    break
                # parse msg
                try:
                    msg = json.loads(raw)
                except Exception:
                    continue

                # handle subscription acks that might arrive later
                if "result" in msg and "id" in msg and not msg.get("method"):
                    # If some ack arrived late (rare), map it (defensive)
                    req_id = msg["id"]
                    subid = msg["result"]
                    if 1 <= req_id <= len(self.wallets):
                        sub_map[subid] = self.wallets[req_id - 1]
                        log.debug("Late ACK mapped %s -> %s", subid, sub_map[subid])
                    continue

                # only care programNotification
                if msg.get("method") != "programNotification":
                    continue

                try:
                    params = msg["params"]
                    sub_id = params.get("subscription")
                    wallet = sub_map.get(sub_id)
                    result = params.get("result") or {}
                    value = result.get("value") or {}
                    account = value.get("account") or {}
                    data_field = account.get("data")
                    # data_field typically: [base64, "base64"]
                    data = data_field[0] if isinstance(data_field, list) else data_field
                    pubkey = value.get("pubkey")
                except Exception as e:
                    log.warning("Malformed notification: %s", e)
                    continue

                mint, owner, amount = parse_token_account(data)
                if not mint:
                    continue

                # quick checks: owner matches wallet and amount == 1
                if owner != wallet:
                    log.debug("Owner mismatch: event owner=%s subscribed_wallet=%s (mint=%s)", owner, wallet, mint)
                    # fallback: even if mismatch, you might still want to process; skip for now
                    continue
                if amount != 1:
                    log.debug("amount != 1, skip (mint=%s amount=%s)", mint, amount)
                    continue

                # dedupe - avoid processing same mint multiple times
                if await self.seen.seen(mint):
                    log.debug("Mint %s already seen recently -> skip", mint)
                    continue
                await self.seen.mark(mint)

                # spawn async processing (non-blocking)
                asyncio.create_task(self._process_mint_task(mint, wallet, pubkey))

    async def _subscribe_all(self, ws, sub_map: Dict[int, str]):
        # send all programSubscribe requests and wait for ACKs (with timeout)
        req_id = 1
        for owner in self.wallets:
            payload = {
                "jsonrpc": "2.0",
                "id": req_id,
                "method": "programSubscribe",
                "params": [
                    self.token_program,
                    {
                        "encoding": "base64",
                        "filters": [
                            {"dataSize": self.token_account_size},
                            {"memcmp": {"offset": 32, "bytes": owner}},
                        ],
                    },
                ],
            }
            await ws.send(json.dumps(payload))
            req_id += 1

        # wait for ACKs until timeout
        start = time.time()
        while len(sub_map) < len(self.wallets):
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=ACK_TIMEOUT)
            except asyncio.TimeoutError:
                # timeout waiting for some ACKs; proceed with whatever mapped
                log.warning("Timeout waiting for ACKs - have %d/%d", len(sub_map), len(self.wallets))
                break
            try:
                msg = json.loads(raw)
            except Exception:
                continue
            if "result" in msg and "id" in msg and not msg.get("method"):
                wallet = self.wallets[msg["id"] - 1]
                sub_id = msg["result"]
                sub_map[sub_id] = wallet
                log.info("Subscribed wallet %s -> sub_id=%s", wallet, sub_id)

    async def _process_mint_task(self, mint: str, wallet: str, token_account_pubkey: str):
        # limit concurrent tasks
        async with self.sem:
            try:
                log.info("Processing mint %s for wallet %s", mint, wallet)
                # run the heavy work in thread to avoid blocking event loop
                result = await asyncio.wait_for(
                    asyncio.to_thread(self._process_mint_sync, mint, wallet, token_account_pubkey),
                    timeout=PROCESS_TIMEOUT
                )
                if result:
                    # insert into DB (assume insert_nft_data is sync) - run in thread as well
                    new_pos_ids = await asyncio.to_thread(insert_nft_data, [result])
                    log.info("Inserted NFT %s into DB (id: %s)", mint, new_pos_ids)
                    
                    await asyncio.to_thread(process_batch_nft_summary, [result], new_pos_ids)
                    log.info("Processed NFT %s summary", mint)
                    
                    await asyncio.to_thread(save_update_status, [wallet], ["SOL"])
                    message = f"✅ Processed mint {mint} for wallet {wallet}."
                    await asyncio.to_thread(send_discord_webhook_message, message, "https://discordapp.com/api/webhooks/1428675664017625121/StXG28M8BV7tmrrmjdsmUL-gJFKUh7f2ZA9EnPHbWpZ3I97tRCB3J1n1YQ9nsBDQBci7")
                else:
                    log.warning("Processing returned no result for mint %s", mint)
            except asyncio.TimeoutError:
                log.warning("Processing mint %s timed out", mint)
            except Exception as e:
                log.exception("Error processing mint %s: %s", mint, e)

    def _process_mint_sync(self, mint: str, wallet: str, token_account_pubkey: str) -> Optional[dict]:
        """
        This runs in a thread. Use existing synchronous functions here:
        - get_all_status_nft_ids_sol(clients, chain_name, [mint])
        - process_nft_mint_data(...)
        Return the final dict ready for insert_nft_data()
        """
        try:
            # 1) reuse existing batch-style function but for single mint
            active, inactived, closed, unknown, status_map, position_map, pool_map = get_all_status_nft_ids_sol(
                self.clients_endpoints, "SOL", [mint]
            )
            # 2) process_nft_mint_data (should return dict suitable for DB)
            nft_result = process_nft_mint_data_sol(
                mint, "SOL", wallet, status_map, position_map, pool_map, inactived_nft_ids=inactived
            )
            print("nft_result type for %s: %s", mint, type(nft_result))
            print("nft_result content: %s", nft_result)
            
            # normalize pubkeys to str inside result (defensive)
            if nft_result:
                if isinstance(nft_result, dict):
                    for k, v in list(nft_result.items()):
                        # convert solders Pubkey or any Pubkey-like objects to string
                        if isinstance(v, Pubkey):
                            nft_result[k] = str(v)
                elif isinstance(nft_result, tuple):
                    nft_result = tuple(str(v) if isinstance(v, Pubkey) else v for v in nft_result)
                    
            return nft_result
        except Exception as e:
            log.exception("Sync processing failed for mint %s: %s", mint, e)
            return None

# --- runner ---
async def main():
    print("Starting SOL NFT Mint Monitor...")
    monitor = NFTWSSMonitor(WALLETS_ADDRESS)
    try:
        await monitor.start()
    except KeyboardInterrupt:
        log.info("Stopping monitor...")
        await monitor.stop()

if __name__ == "__main__":
    asyncio.run(main())
